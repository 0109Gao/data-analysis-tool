"""数据清洗自检：每个动作都要能验证「改了什么、改了几行」，并且默认不越权改数据。"""

from __future__ import annotations

import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from glassdata.clean import CleanOptions, clean_table, write_clean_csv  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("  ok   " + name)
    else:
        FAILURES.append(name)
        print("  FAIL " + name + (("  :: " + str(detail)) if detail is not None else ""))


COLUMNS = ["区域", "金额", "评分"]
ROWS = [
    ["  华东  ", "1,234", "4.5"],
    ["华东", "¥99.00", ""],
    ["华东", "¥99.00", ""],          # 与上一行完全重复
    ["", "", ""],                     # 整行为空
    ["华南\u3000", "(20)", "3.0"],    # 全角空格 + 会计负数
    ["华北", "", "2.0"],              # 金额缺失
]


def step_of(report, name):
    for step in report["steps"]:
        if step["name"] == name:
            return step
    return {}


def main():
    result = clean_table(COLUMNS, ROWS)
    report = result.report

    check("默认策略会去掉首尾空白", result.rows[0][0] == "华东", result.rows[0])
    check("全角空格也算空白", result.rows[2][0] == "华南", result.rows[2])
    check("空行被删除", len(result.rows) == 4, len(result.rows))
    check("完全重复行被删除", len(result.rows) == 4 and result.rows[1] == ["华东", "99", ""], result.rows)
    check("千分位被归一", result.rows[0][1] == "1234", result.rows[0][1])
    check("货币符号被去掉", result.rows[1][1] == "99", result.rows[1][1])
    check("会计负数转成 -20", result.rows[2][1] == "-20", result.rows[2][1])
    check("空值仍然是空值（默认不填补）", result.rows[3][1] == "", repr(result.rows[3][1]))
    check("默认不动列结构", result.columns == COLUMNS, result.columns)

    before, after = report["before"], report["after"]
    check("清洗前行数记录正确", before["rows"] == 6, before)
    check("清洗后行数记录正确", after["rows"] == 4, after)
    check("删掉的行数记在报告里", report["removed_rows"] == 2, report["removed_rows"])
    check("每步都有日志", len(report["steps"]) >= 4, len(report["steps"]))
    check("去重日志写清洗了几行", step_of(report, "去重")["changed"] == 1, step_of(report, "去重"))
    check("日志里不出现 None", all("None" not in str(step) for step in report["steps"]), report["steps"])

    # 百分比不该被悄悄换算成小数
    pct = clean_table(["折扣"], [["12%"], ["5%"]])
    check("百分比保持原样", pct.rows == [["12%"], ["5%"]], pct.rows)

    # 均值 / 中位数 / 众数 / 前值 / 固定值 / 删行
    base_cols = ["区域", "金额"]
    base_rows = [["华东", "10"], ["华北", ""], ["华南", "30"], ["华中", ""]]
    filled = clean_table(base_cols, base_rows, CleanOptions(fill="mean"))
    # 10 和 30 的均值是 20
    check("均值填补", filled.rows[1][1] == "20" and filled.rows[3][1] == "20", filled.rows)
    check("填补会记格数", filled.report["filled_cells"] == 2, filled.report["filled_cells"])

    median = clean_table(base_cols, base_rows, CleanOptions(fill="median"))
    check("中位数填补", median.rows[1][1] == "20", median.rows)

    mode = clean_table(["区域", "渠道"], [["华东", "官网"], ["华南", ""], ["华东", ""]], CleanOptions(fill="mode"))
    check("众数填补（类别列）", mode.rows[1][1] == "官网" and mode.rows[2][1] == "官网", mode.rows)

    ffill = clean_table(base_cols, base_rows, CleanOptions(fill="ffill"))
    check("前值填补", ffill.rows[1][1] == "10" and ffill.rows[3][1] == "30", ffill.rows)

    fixed = clean_table(base_cols, base_rows, CleanOptions(fill="value", fill_value="0"))
    check("固定值填补", fixed.rows[1][1] == "0", fixed.rows)

    dropped = clean_table(base_cols, base_rows, CleanOptions(fill="drop"))
    check("删除含缺失的行", len(dropped.rows) == 2, dropped.rows)
    check("删行日志写明原因", "缺失" in step_of(dropped.report, "缺失处理")["detail"],
          step_of(dropped.report, "缺失处理"))

    # 异常值：10 个正常值 + 1 个离谱值
    normal = [["%d" % (index + 1)] for index in range(10)] + [["9999"]]
    flagged = clean_table(["值"], normal, CleanOptions(fill="none", outliers="flag"))
    check("IQR 标记新增一列", flagged.columns == ["值", "值_异常"], flagged.columns)
    check("IQR 标记出那一个离谱值", flagged.rows[-1][1] == "是", flagged.rows[-1])
    check("正常值不被标记", all(row[1] == "否" for row in flagged.rows[:10]), flagged.rows[:3])

    removed = clean_table(["值"], normal, CleanOptions(fill="none", outliers="drop"))
    check("IQR 可删除极端行", len(removed.rows) == 10, len(removed.rows))

    # 清洗不改原表（避免调用方拿去别处用时被副作用坑到）
    source = [["华东", " 1 "]]
    clean_table(["区域", "值"], source)
    check("不修改传入的原始行", source == [["华东", " 1 "]], source)

    # 导出
    workdir = tempfile.mkdtemp(prefix="glassdata_clean_")
    try:
        path = os.path.join(workdir, "清洗结果.csv")
        write_clean_csv(path, result.columns, result.rows)
        with open(path, "rb") as handle:
            head = handle.read(3)
        check("导出带 UTF-8 BOM（Excel 双击不乱码）", head == b"\xef\xbb\xbf", head)
        with open(path, encoding="utf-8-sig", newline="") as handle:
            back = list(csv.reader(handle))
        check("导出内容可回读", back[0] == COLUMNS and back[1][0] == "华东", back[:2])
    finally:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)

    print("")
    if FAILURES:
        print("FAILED: " + ", ".join(FAILURES))
        return 1
    print("ALL_CLEAN_CHECKS_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
