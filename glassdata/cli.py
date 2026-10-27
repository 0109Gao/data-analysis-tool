"""命令行入口：读取表格 → 画像 → 输出自包含 HTML 报告。"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from typing import List, Optional, Sequence

from .clean import CleanOptions, clean_table, write_clean_csv
from .core import analyze_table, read_table
from .excel import list_sheets
from .render import write_report
from .sample import write_demo_csv


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glassdata",
        description="液态玻璃风格的数据处理与可视化报告生成器（纯标准库实现）",
        epilog=(
            "示例：\n"
            "  python -m glassdata data.csv -o report.html\n"
            "  python -m glassdata --demo -o demo.html --open"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", nargs="?", help="数据文件路径（Excel / CSV / TSV / JSON）")
    parser.add_argument("-o", "--output", help="输出 HTML 路径，默认与输入文件同目录")
    parser.add_argument("-t", "--title", help="报告标题")
    parser.add_argument(
        "--preview-rows",
        type=int,
        default=40,
        help="内嵌到报告的预览行数（默认 40）",
    )
    parser.add_argument("--demo", action="store_true", help="使用内置合成示例数据")
    parser.add_argument("--demo-csv", help="把示例数据另存为指定 CSV 路径")
    parser.add_argument(
        "--date-order",
        choices=("dmy", "mdy"),
        default="dmy",
        help="dd/mm/yyyy 这类写法的解释顺序：dmy=日/月/年（默认），mdy=月/日/年",
    )
    parser.add_argument(
        "--sheet",
        help="Excel 工作表：名称或从 1 开始的序号（默认第一个工作表）",
    )
    parser.add_argument(
        "--clean",
        metavar="PATH",
        help="导出清洗后的 CSV 到指定路径（不填则只在报告里体现清洗结果）",
    )
    parser.add_argument(
        "--fill",
        choices=("none", "drop", "mean", "median", "mode", "ffill", "value"),
        default="none",
        help="缺失值处理方式（默认 none：只标记不填补）",
    )
    parser.add_argument("--fill-value", default="", help="--fill value 时使用的固定值")
    parser.add_argument(
        "--outliers",
        choices=("none", "flag", "drop"),
        default="none",
        help="IQR 异常值处理：flag=加标记列，drop=删除所在行（默认 none）",
    )
    parser.add_argument("--keep-duplicates", action="store_true", help="清洗时保留完全重复的行")
    parser.add_argument("--keep-whitespace", action="store_true", help="清洗时保留单元格首尾空白")
    parser.add_argument("--open", action="store_true", help="生成后在默认浏览器打开")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.input and not args.demo:
        parser.print_help()
        return 2

    if args.demo:
        csv_path = args.demo_csv or os.path.join(os.getcwd(), "examples", "demo_orders.csv")
        source_path = write_demo_csv(csv_path)
        print(f"[glassdata] 已生成合成示例数据：{source_path}")
    else:
        source_path = args.input

    sheets = list_sheets(source_path) if source_path else []
    if len(sheets) > 1 and not args.sheet:
        print("[glassdata] 工作簿含 %d 个工作表：%s（默认读第一个，可用 --sheet 指定）"
              % (len(sheets), "、".join(sheets)))

    try:
        columns, rows, display_name = read_table(source_path, sheet=args.sheet)
    except (FileNotFoundError, ValueError) as error:
        print(f"[glassdata] 读取失败：{error}", file=sys.stderr)
        return 1

    cleaning = None
    wants_clean = bool(args.clean) or args.fill != "none" or args.outliers != "none" \
        or args.keep_duplicates or args.keep_whitespace
    if wants_clean:
        options = CleanOptions(
            trim=not args.keep_whitespace,
            dedupe=not args.keep_duplicates,
            fill=args.fill,
            fill_value=args.fill_value,
            outliers=args.outliers,
        )
        result = clean_table(columns, rows, options)
        columns, rows = result.columns, result.rows
        cleaning = result.report
        before, after = cleaning["before"], cleaning["after"]
        print("[glassdata] 已清洗：行 %d → %d，缺失 %d → %d（%s）"
              % (before["rows"], after["rows"], before["missing"], after["missing"],
                 "、".join(cleaning["options"]) or "无动作"))
        for step in cleaning["steps"]:
            print("  · %s：%s" % (step["name"], step["detail"]))

    payload = analyze_table(
        columns,
        rows,
        source=display_name,
        engine="python",
        preview_rows=max(1, args.preview_rows),
        title=args.title,
        date_order=args.date_order,
    )
    if cleaning:
        payload["cleaning"] = cleaning

    if args.output:
        output_path = args.output
    else:
        stem = os.path.splitext(os.path.basename(source_path))[0]
        output_path = os.path.join(
            os.path.dirname(os.path.abspath(source_path)) or ".", f"{stem}_report.html"
        )

    written = write_report(payload, output_path, title=args.title)

    meta = payload["meta"]
    quality = payload["quality"]
    print(f"[glassdata] 行数 {meta['rows']} · 列数 {meta['cols']} · "
          f"完整度 {quality['completeness'] * 100:.2f}%")
    for insight in payload["insights"]:
        print(f"  · {insight['text']}")
    for warning in payload.get("warnings", []):
        mark = "!" if warning.get("level") == "warn" else "-"
        print(f"  {mark} {warning['message']}")
    if any(w.get("kind") == "date-order" for w in payload.get("warnings", [])):
        print("    提示：用 --date-order mdy 可切换为「月/日/年」解释后重新生成")
    print(f"[glassdata] 报告已写入：{written}")

    if args.clean:
        try:
            stats = {}
            clean_path = write_clean_csv(args.clean, columns, rows, stats)
            print(f"[glassdata] 清洗后的数据已导出：{clean_path}")
            if stats.get("escaped"):
                print(
                    "[glassdata] 其中 %d 个单元格以 = + - @ 开头，已加单引号前缀，"
                    "避免 Excel 打开时当公式执行（负号开头的数字不受影响）"
                    % stats["escaped"]
                )
        except OSError as error:
            print(f"[glassdata] 清洗结果导出失败：{error}", file=sys.stderr)
            return 1

    if args.open:
        webbrowser.open(f"file:///{written.replace(os.sep, '/')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
