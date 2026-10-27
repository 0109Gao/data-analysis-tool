"""glassdata 引擎自检：输入格式、编码、边界数据。"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from glassdata.core import analyze_table, parse_date, parse_number, read_table
from glassdata.render import build_html

failures = []


def check(name, condition, detail=""):
    if condition:
        print("  ok   " + name)
    else:
        failures.append(name)
        print("  FAIL " + name + (" :: " + str(detail) if detail else ""))


tmp = tempfile.mkdtemp(prefix="glassdata_")


def write(name, content, encoding="utf-8"):
    path = os.path.join(tmp, name)
    with io.open(path, "w", encoding=encoding, newline="") as handle:
        handle.write(content)
    return path


print("A. 解析器单测")
check("千分位", parse_number("1,234.5") == 1234.5)
check("货币符号", parse_number("¥1,200") == 1200.0)
check("百分号", parse_number("12.5%") == 0.125)
check("会计负数", parse_number("(300)") == -300.0)
check("空字符串为 None", parse_number("") is None)
check("非数字为 None", parse_number("abc") is None)
check("日期 ISO", parse_date("2026-03-04") is not None)
check("日期带时间", parse_date("2026-03-04 05:06:07") is not None)
check("日期带时区", parse_date("2026-03-04T05:06:07+08:00") is not None)
check("非日期为 None", parse_date("not-a-date") is None)

print("B. 输入格式")
csv_path = write("a.csv", "x,y\n1,2\n3,4\n")
cols, rows, name = read_table(csv_path)
check("CSV 读取", cols == ["x", "y"] and len(rows) == 2, (cols, rows))

tsv_path = write("b.tsv", "x\ty\n1\t2\n3\t4\n")
cols, rows, _ = read_table(tsv_path)
check("TSV 自动识别分隔符", cols == ["x", "y"] and len(rows) == 2, (cols, rows))

gbk_path = write("c.csv", "名称,数量\n苹果,3\n香蕉,5\n", encoding="gbk")
cols, rows, _ = read_table(gbk_path)
check("GBK 编码回退", cols == ["名称", "数量"] and rows[0][0] == "苹果", (cols, rows))

# 编码判定顺序（BOM → UTF-16 特征 → UTF-8 → GB18030/Big5 打分）。
# 这几条都是为了挡住"GB18030 抢先解成功、结果全是乱码"这个静默失败：
# 乱码不会抛异常，只会让用户看到一份莫名其妙的报告。
def read_bytes(name, data):
    path = os.path.join(tmp, name)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


_txt = "名称,数量\n苹果,3\n香蕉,5\n"

_cases = [
    ("UTF-16 LE 带 BOM", _txt.encode("utf-16"), "名称"),
    ("UTF-16 BE 带 BOM", b"\xfe\xff" + _txt.encode("utf-16-be"), "名称"),
    ("UTF-16 LE 无 BOM", _txt.encode("utf-16-le"), "名称"),
    ("UTF-16 BE 无 BOM", _txt.encode("utf-16-be"), "名称"),
    ("UTF-32 带 BOM", _txt.encode("utf-32"), "名称"),
    ("UTF-8 BOM", _txt.encode("utf-8-sig"), "名称"),
    ("Big5 繁体", "名稱,數量\n蘋果,3\n香蕉,5\n".encode("big5"), "名稱"),
]
for _name, _data, _head in _cases:
    _p = read_bytes("enc_%s.csv" % abs(hash(_name)), _data)
    try:
        _cols, _rows, _ = read_table(_p)
        check("编码可正确识别：" + _name, _cols[0] == _head and _rows[0][0] in ("苹果", "蘋果"),
              str((_cols, _rows[:1])))
    except Exception as _exc:  # noqa: BLE001
        check("编码可正确识别：" + _name, False, "%s: %s" % (type(_exc).__name__, _exc))

semi_path = write("d.csv", "x;y\n1;2\n")
cols, rows, _ = read_table(semi_path)
check("分号分隔", cols == ["x", "y"], cols)

json_path = write(
    "e.json",
    json.dumps([{"a": 1, "b": {"nested": 2}}, {"a": 3, "c": None}], ensure_ascii=False),
)
cols, rows, _ = read_table(json_path)
check("JSON 键并集", cols == ["a", "b", "c"], cols)
# 嵌套值序列化成紧凑 JSON（无空格），与前端 JSON.stringify 保持字节一致：
# 两边格式不同的话，同一份数据在 CLI 报告和网页里会长得不一样。
check("JSON 嵌套对象序列化", rows[0][1] == '{"nested":2}', rows[0][1])
check("JSON null 转空串", rows[1][2] == "", rows[1][2])

# 每行一个 JSON：日志导出、接口批量导出最常见的就是这个格式。
# 它若被当成分隔文本，列名会变成 ['{"a":1', 'b:2}'] 这种碎片，不报错也没法用。
cols, rows, _ = read_table(write("f.jsonl", '{"a":1,"b":"x"}\n{"a":2,"c":3}\n'))
check("JSONL 键并集", cols == ["a", "b", "c"], cols)
check("JSONL 行数", len(rows) == 2, len(rows))
check("JSONL 缺键补空", rows[1][1] == "", rows[1])

cols, rows, _ = read_table(write("g.ndjson", "[1,2]\n[3]\n"))
check("NDJSON 数组行", cols == ["col_1", "col_2"] and rows[1] == ["3", ""], (cols, rows))

try:
    read_table(write("h.jsonl", '{"a":1}\n{"a":\n'))
    check("JSONL 坏行报错", False, "没有报错")
except ValueError as exc:
    check("JSONL 坏行指出行号", "第 2 行" in str(exc), str(exc))

# 二进制格式靠文件头识别，不靠扩展名：用户常把 parquet 改名成 .csv。
# 只认扩展名的话，这类文件会一路走到「文件只有表头」，把责任推给用户。
def write_bytes(name, blob):
    path = os.path.join(tmp, name)
    with open(path, "wb") as handle:
        handle.write(blob)
    return path


BINARY_CASES = {
    "parquet": (b"PAR1\x15\x00\x2cbinary", "Parquet"),
    "sqlite": (b"SQLite format 3\x00" + b"\x00" * 24, "SQLite"),
    "stata": (b"<stata_dta><header>", "Stata"),
    "spss": (b"$FL2" + b"\x00" * 24, "SPSS"),
    "sas": (b"\x00" * 12 + b"\xc2\xea\x81\x60" + b"\x00" * 12, "SAS"),
    "hdf5": (b"\x89HDF\r\n\x1a\n" + b"\x00" * 16, "HDF5"),
    "gzip": (b"\x1f\x8b\x08\x00payload", "gzip"),
    "pdf": (b"%PDF-1.7\n...", "PDF"),
    "png": (b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "PNG"),
}
for name, (blob, hint) in BINARY_CASES.items():
    try:
        read_table(write_bytes("bin_%s.csv" % name, blob))   # 故意用 .csv 后缀
        check("二进制识别：%s" % name, False, "没有报错")
    except ValueError as exc:
        text = str(exc)
        actionable = any(word in text for word in ("另存为", "导出", "解压"))
        check("二进制识别：%s" % name, hint in text and actionable, text)

# 未知二进制格式没有固定文件头，只能靠「不可打印字符占比」兜底。
try:
    read_table(write_bytes("bin_unknown.csv", bytes(range(1, 9)) * 200))
    check("未知二进制兜底", False, "没有报错")
except ValueError as exc:
    check("未知二进制兜底", "不可打印字符" in str(exc), str(exc))

print("C. 边界数据集")
payload = analyze_table(["only"], [["1"], ["2"], ["3"]], source="one.csv")
check("单列单类型", payload["columns"][0]["type"] == "number")
check("单列完整度", payload["quality"]["completeness"] == 1.0)

payload = analyze_table(["a", "b"], [["", ""], ["", ""]], source="blank.csv")
check("全空数据集不抛错", all(c["type"] == "empty" for c in payload["columns"]))
check("全空完整度为 0", payload["quality"]["completeness"] == 0.0)
check("全空相关性为空", payload["correlation"]["labels"] == [])

payload = analyze_table(["x"], [["1"], ["1"], ["1"]], source="const.csv")
check("常量列标准差为 0", payload["columns"][0]["stats"]["std"] == 0.0)
check("常量列方差为 0 时自相关记为未定义", payload["correlation"]["matrix"][0][0] is None)

payload = analyze_table(["x"], [["1"], ["1"], ["2"]], source="dup.csv")
check("重复行统计（3 行中有 1 行与前文重复）", payload["quality"]["duplicateRows"] == 1, payload["quality"]["duplicateRows"])

payload = analyze_table(["t"], [["2026-01-01 08:00"], ["2026-06-01 09:00"]], source="time.csv")
check("时间列跨度", abs(payload["columns"][0]["stats"]["spanDays"] - 151.04) < 0.1, payload["columns"][0]["stats"])

print("D. 相关性对齐（两列缺失位置不同）")
rows = [
    ["", "1"],
    ["2", "2"],
    ["4", ""],
    ["6", "4"],
    ["8", "5"],
]
payload = analyze_table(["a", "b"], rows, source="align.csv")
r = payload["correlation"]["matrix"][0][1]
check("缺失行按行号对齐（r≈1）", r is not None and abs(r - 1.0) < 0.05, r)

print("E. HTML 渲染")
empty_html = build_html({})
check("空 payload 可渲染", "__PAYLOAD__" not in empty_html and "__TITLE__" not in empty_html)
check("空 payload 内嵌 {} ", '"quality"' not in empty_html)

payload = analyze_table(["x"], [["1"], ["2"]], source="render.csv", title="自定义标题")
markup = build_html(payload)
check("标题注入", "自定义标题" in markup)
check("数据注入", '"render.csv"' in markup)
check("脚本闭合未被破坏", markup.count("<script") == markup.count("</script>"))

risky = analyze_table(["x"], [["</script><script>alert(1)</script>"]], source="xss.csv")
markup = build_html(risky)
check("数据中的尖括号全被转义", markup.count("<script") == 2 and markup.count("</script>") == 2, str((markup.count("<script"), markup.count("</script>"))))
check("原始攻击载荷未出现", "</script><script>alert(1)" not in markup)

print("F. 日期顺序与歧义告警")
from glassdata.core import date_order_ambiguity, parse_date

amb_rows = [["01/05/2026"], ["03/04/2026"], ["25/12/2026"]]
dmy = analyze_table(["when"], amb_rows, source="amb.csv", date_order="dmy")
mdy = analyze_table(["when"], amb_rows, source="amb.csv", date_order="mdy")
check("dmy 解析为 5 月 1 日", dmy["columns"][0]["stats"]["min"] == "2026-04-03 00:00", dmy["columns"][0]["stats"]["min"])
check("mdy 解析为 1 月 5 日", mdy["columns"][0]["stats"]["min"] == "2026-01-05 00:00", mdy["columns"][0]["stats"]["min"])
check("两种顺序结论不同", dmy["columns"][0]["stats"]["spanDays"] != mdy["columns"][0]["stats"]["spanDays"],
      f"{dmy['columns'][0]['stats']['spanDays']} vs {mdy['columns'][0]['stats']['spanDays']}")
check("歧义计数为 2", date_order_ambiguity(["01/05/2026", "03/04/2026", "25/12/2026", "5/5/2026"]) == 2,
      str(date_order_ambiguity(["01/05/2026", "03/04/2026", "25/12/2026", "5/5/2026"])))
check("无歧义写法不计数", date_order_ambiguity(["2026-01-05", "13/05/2026", "2026-02-11"]) == 0)
check("mdy 下非法月退回日/月", parse_date("25/12/2026", "mdy").month == 12)
check("meta 记录日期顺序", dmy["meta"]["dateOrder"] == "dmy")
check("歧义列产出告警", any(w["kind"] == "date-order" for w in dmy["warnings"]), str(dmy["warnings"]))
check("告警计入 quality", dmy["quality"]["warnings"] == len(dmy["warnings"]))
check("非歧义数据不产告警", not any(w["kind"] == "date-order" for w in
      analyze_table(["d"], [["2026-01-05"]], source="ok.csv")["warnings"]))
check("常量列产出提示", any(w["kind"] == "constant-column" for w in
      analyze_table(["a", "b"], [["1", "x"], ["2", "x"]], source="c.csv")["warnings"]))
check("高缺失列产出告警", any(w["kind"] == "high-missing" for w in
      analyze_table(["a"], [["1"], [""], [""], ["4"]], source="m.csv")["warnings"]))

print()
if failures:
    print("FAILURES: " + ", ".join(failures))
    sys.exit(1)
print("ALL_ENGINE_CHECKS_PASSED")
