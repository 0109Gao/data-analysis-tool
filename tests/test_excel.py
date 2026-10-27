"""Excel / ODS 读取自检：用 zipfile 现场造真实工作簿，再读回来比对。

不依赖任何测试夹具文件：夹具就是代码，跑一次生成一次，改了实现也不会与夹具脱节。
覆盖共享字符串、内联字符串、布尔、错误值、日期序列号（含 1900 闰年 bug）、
1904 基准、多工作表选择、空行与参差行、改名成 .csv 的伪装。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import zipfile
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from glassdata.core import read_table  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("  ok   " + name)
    else:
        FAILURES.append(name)
        print("  FAIL " + name + (("  :: " + str(detail)) if detail is not None else ""))


# --------------------------------------------------------------------------- #
# 现场造一个 .xlsx（最小但结构合法）
# --------------------------------------------------------------------------- #
CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy&quot;年&quot;m&quot;月&quot;d&quot;日&quot;"/></numFmts>
<cellXfs count="3">
<xf numFmtId="0"/>
<xf numFmtId="14" applyNumberFormat="1"/>
<xf numFmtId="164" applyNumberFormat="1"/>
</cellXfs>
</styleSheet>"""


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _serial(value, date1904=False):
    base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
    if isinstance(value, datetime):
        delta = value - base
        return repr(delta.days + delta.seconds / 86400.0)
    return repr(float((value - base.date()).days))


def _col(index):
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def build_xlsx(path, sheets, date1904=False):
    """sheets: [(名称, [[值...], ...])]，值可为 str/int/float/date/datetime/bool/None/("inline", 文本)/("error", 文本)"""
    shared = []
    index_of = {}

    def shared_index(text):
        if text not in index_of:
            index_of[text] = len(shared)
            shared.append(text)
        return index_of[text]

    sheet_xml = []
    for _, rows in sheets:
        body = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, value in enumerate(row):
                ref = "%s%d" % (_col(c), r)
                if value is None or value == "":
                    continue
                if isinstance(value, tuple):
                    kind, payload = value
                    if kind == "inline":
                        cells.append('<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, _esc(payload)))
                    else:
                        cells.append('<c r="%s" t="e"><v>%s</v></c>' % (ref, _esc(payload)))
                elif isinstance(value, bool):
                    cells.append('<c r="%s" t="b"><v>%d</v></c>' % (ref, 1 if value else 0))
                elif isinstance(value, (date, datetime)):
                    style = 2 if isinstance(value, datetime) else 1
                    cells.append('<c r="%s" s="%d"><v>%s</v></c>' % (ref, style, _serial(value, date1904)))
                elif isinstance(value, (int, float)):
                    cells.append('<c r="%s"><v>%s</v></c>' % (ref, repr(value)))
                else:
                    cells.append('<c r="%s" t="s"><v>%d</v></c>' % (ref, shared_index(str(value))))
            body.append('<row r="%d">%s</row>' % (r, "".join(cells)))
        sheet_xml.append(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData>%s</sheetData></worksheet>" % "".join(body)
        )

    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        + ('<workbookPr date1904="1"/>' if date1904 else "")
        + "<sheets>"
        + "".join(
            '<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (_esc(name), i + 1, i + 1)
            for i, (name, _) in enumerate(sheets)
        )
        + "</sheets></workbook>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1)
            for i in range(len(sheets))
        )
        + "</Relationships>"
    )
    strings = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="%d" uniqueCount="%d">%s</sst>'
        % (len(shared), len(shared), "".join("<si><t>%s</t></si>" % _esc(text) for text in shared))
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CT)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/styles.xml", STYLES)
        archive.writestr("xl/sharedStrings.xml", strings)
        for i, xml in enumerate(sheet_xml, 1):
            archive.writestr("xl/worksheets/sheet%d.xml" % i, xml)


ODS_CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
  xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
  xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
  xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:spreadsheet>
  <table:table table:name="销售">
   <table:table-row>
    <table:table-cell office:value-type="string"><text:p>区域</text:p></table:table-cell>
    <table:table-cell office:value-type="string"><text:p>金额</text:p></table:table-cell>
    <table:table-cell office:value-type="string"><text:p>日期</text:p></table:table-cell>
   </table:table-row>
   <table:table-row>
    <table:table-cell office:value-type="string"><text:p>华东</text:p></table:table-cell>
    <table:table-cell office:value-type="float" office:value="128.5"><text:p>128.5</text:p></table:table-cell>
    <table:table-cell office:value-type="date" office:date-value="2026-03-01"><text:p>2026-03-01</text:p></table:table-cell>
   </table:table-row>
   <table:table-row>
    <table:table-cell office:value-type="string"><text:p>华南</text:p></table:table-cell>
    <table:table-cell office:value-type="float" office:value="99"><text:p>99</text:p></table:table-cell>
    <table:table-cell office:value-type="date" office:date-value="2026-03-02"><text:p>2026-03-02</text:p></table:table-cell>
   </table:table-row>
  </table:table>
  <table:table table:name="空表">
   <table:table-row><table:table-cell/></table:table-row>
  </table:table>
 </office:spreadsheet></office:body>
</office:document-content>"""


def build_ods(path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        archive.writestr("content.xml", ODS_CONTENT)


def main():
    workdir = tempfile.mkdtemp(prefix="glassdata_excel_")
    try:
        books = os.path.join(workdir, "订单.xlsx")
        build_xlsx(books, [
            ("一月", [
                ["订单号", "区域", "数量", "金额", "下单日期", "已付款", "备注"],
                ["A001", "华东", 3, 128.5, date(2026, 3, 1), True, "首单"],
                ["A002", "华南", 1, 66.0, date(2026, 3, 2), False, ("inline", "内联文本")],
                ["A003", "华东", 12, 999.99, datetime(2026, 3, 5, 14, 30), True, ("error", "#DIV/0!")],
                ["", "", "", "", "", "", ""],           # 整行空 → 应被丢掉
                ["A005", "华北", 2, 20.4, date(2026, 3, 9), True],   # 参差行 → 应补齐
            ]),
            ("二月", [
                ["订单号", "区域", "数量"],
                ["B001", "西南", 7],
                ["B002", "华中", 4],
            ]),
        ])

        columns, rows, display = read_table(books)
        check("xlsx 读到表头", columns == ["订单号", "区域", "数量", "金额", "下单日期", "已付款", "备注"], columns)
        check("空行被丢弃、参差行被补齐", len(rows) == 4 and len(rows[3]) == 7, rows[-1] if rows else None)
        check("默认取第一个工作表", display.endswith("· 一月"), display)
        check("共享字符串读取正确", rows[0][0] == "A001" and rows[0][1] == "华东", rows[0])
        check("整数不带小数点", rows[0][2] == "3" and rows[2][2] == "12", rows[0][2])
        check("小数不丢精度", rows[0][3] == "128.5" and rows[2][3] == "999.99", rows[0][3])
        check("日期格式化为 YYYY-MM-DD", rows[0][4] == "2026-03-01", rows[0][4])
        check("带时间的日期保留时刻", rows[2][4] == "2026-03-05 14:30", rows[2][4])
        check("布尔读成 TRUE/FALSE", rows[0][5] == "TRUE" and rows[1][5] == "FALSE", rows[1][5])
        check("内联字符串可读", rows[1][6] == "内联文本", rows[1][6])
        check("错误值原样保留", rows[2][6] == "#DIV/0!", rows[2][6])
        check("空单元格补空串", rows[3][6] == "", repr(rows[3][6]))

        _, rows2, display2 = read_table(books, sheet="二月")
        check("按名字选工作表", rows2[0][0] == "B001" and "二月" in display2, display2)
        _, rows3, display3 = read_table(books, sheet=2)
        check("按序号选工作表（1 起）", rows3[0][0] == "B001", display3)

        try:
            read_table(books, sheet="不存在")
        except ValueError as exc:
            check("选错工作表给出可选清单", "二月" in str(exc), str(exc))
        else:
            check("选错工作表给出可选清单", False, "未抛错")

        # 改名成 .csv：只认扩展名就会掉进乱码解析
        disguise = os.path.join(workdir, "伪装.csv")
        shutil.copyfile(books, disguise)
        columns_d, rows_d, _ = read_table(disguise)
        check("改名成 .csv 仍按工作簿解析", columns_d[0] == "订单号" and rows_d[0][0] == "A001", columns_d[:2])

        # 1904 基准（Mac 版 Excel 的老习惯）
        mac = os.path.join(workdir, "mac.xlsx")
        build_xlsx(mac, [("表1", [["日期"], [date(2026, 3, 1)]])], date1904=True)
        _, mac_rows, _ = read_table(mac)
        check("1904 日期基准正确", mac_rows[0][0] == "2026-03-01", mac_rows[0][0])

        # 旧版 .xls：必须给出可执行的下一步，而不是一句「不支持」
        legacy = os.path.join(workdir, "旧版.xls")
        with open(legacy, "wb") as handle:
            handle.write(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
        try:
            read_table(legacy)
        except ValueError as exc:
            check("旧版 xls 提示另存为 xlsx/CSV", "另存为" in str(exc), str(exc))
        else:
            check("旧版 xls 提示另存为 xlsx/CSV", False, "未抛错")

        # 只有表头的工作表
        empty = os.path.join(workdir, "只有表头.xlsx")
        build_xlsx(empty, [("表1", [["a", "b"]])])
        try:
            read_table(empty)
        except ValueError as exc:
            check("只有表头时给出明确提示", "只有表头" in str(exc), str(exc))
        else:
            check("只有表头时给出明确提示", False, "未抛错")

        # ODS
        ods = os.path.join(workdir, "报表.ods")
        build_ods(ods)
        columns_o, rows_o, display_o = read_table(ods)
        check("ods 表头正确", columns_o == ["区域", "金额", "日期"], columns_o)
        check("ods 数值与日期正确", rows_o[0][1] == "128.5" and rows_o[1][2] == "2026-03-02", rows_o[1])
        check("ods 多表时显示工作表名", "销售" in display_o, display_o)

        from glassdata.excel import list_sheets

        check("list_sheets 列出全部工作表", list_sheets(books) == ["一月", "二月"], list_sheets(books))

        # 端到端：xlsx → 清洗 → 报告文件（双击入口 build_report 的同一条链路）
        from main import build_report

        dirty = os.path.join(workdir, "脏数据.xlsx")
        build_xlsx(dirty, [("表1", [
            ["区域", "金额", "渠道"],
            [" 华东 ", "1,200", "官网"],
            ["华东", "1,200", "官网"],
            ["华南", "", "小程序"],
            ["", "", ""],
            ["华北", "¥88.00", "官网"],
        ])])
        from glassdata.clean import CleanOptions

        result = build_report(
            dirty,
            output=os.path.join(workdir, "报告.html"),
            options=CleanOptions(fill="mode"),
            clean_csv=os.path.join(workdir, "清洗后.csv"),
            open_after=False,
        )
        check("端到端：报告文件已生成", os.path.exists(result["report"]), result["report"])
        with open(result["report"], encoding="utf-8") as handle:
            markup = handle.read()
        check("报告里带清洗摘要", "已清洗" in markup and "clean-strip" in markup, "缺少清洗卡片")
        check("报告标题用中文数据分析", "<title>脏数据.xlsx · 数据分析</title>" in markup,
              markup.split("<title>")[1].split("</title>")[0] if "<title>" in markup else "")
        check("端到端：清洗后行数正确", result["payload"]["meta"]["rows"] == 3,
              result["payload"]["meta"]["rows"])
        check("端到端：清洗 CSV 已导出", os.path.exists(result["clean_csv"]), result["clean_csv"])
        # Excel 读取阶段已经丢掉整行为空的行，所以清洗前是 4 行；去重后 3 行
        check("清洗前后统计进入报告数据",
              result["cleaning"]["before"]["rows"] == 4 and result["cleaning"]["after"]["rows"] == 3,
              result["cleaning"]["before"])

    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("")
    if FAILURES:
        print("FAILED: " + ", ".join(FAILURES))
        return 1
    print("ALL_EXCEL_CHECKS_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
