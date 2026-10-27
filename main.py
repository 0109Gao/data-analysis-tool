#!/usr/bin/env python3
"""数据分析工具 · 双击就能用的入口。

两种用法：

1) 双击本文件（或在终端执行 `python main.py`）→ 弹出小窗口：
   选文件 → 选工作表 → 勾清洗选项 → 生成报告，报告用默认浏览器自动打开。

2) 带参数执行时直接走命令行，等价于 `python -m glassdata`：
   python main.py 订单.xlsx --sheet 一月 --fill mean --clean 清洗后.csv -o 报告.html

只依赖 Python 标准库：没有 tkinter 的环境（精简版 Python、无图形界面的服务器）
会自动退回到命令行提示，不会崩在半路。
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 不生成 __pycache__：这个目录是要拷来拷去的交付物，
# 多出一个字节码缓存文件夹只会让人怀疑「是不是又留下旧东西了」。
sys.dont_write_bytecode = True

from glassdata.clean import CleanOptions, clean_table, write_clean_csv  # noqa: E402
from glassdata.core import analyze_table, read_table  # noqa: E402
from glassdata.excel import list_sheets  # noqa: E402
from glassdata.render import write_report  # noqa: E402

SUPPORTED_HINT = "Excel（.xlsx / .xlsm / .ods）· CSV / TSV / TXT · JSON / JSONL"
FILE_TYPES = [
    ("表格文件", "*.xlsx *.xlsm *.ods *.csv *.tsv *.txt *.json *.jsonl *.ndjson"),
    ("Excel 工作簿", "*.xlsx *.xlsm *.ods"),
    ("分隔文本", "*.csv *.tsv *.txt"),
    ("JSON", "*.json *.jsonl *.ndjson"),
    ("全部文件", "*.*"),
]
FILL_LABELS = [
    ("不填补（只标记缺失）", "none"),
    ("删除含缺失的行", "drop"),
    ("用均值填补数值列", "mean"),
    ("用中位数填补数值列", "median"),
    ("用众数填补", "mode"),
    ("用上一个非空值填补", "ffill"),
]
OUTLIER_LABELS = [
    ("只标记（新增一列「_异常」）", "flag"),
    ("删除含极端值的行", "drop"),
    ("不处理", "none"),
]


def build_report(
    path: str,
    output: str = "",
    sheet: str = "",
    options: CleanOptions | None = None,
    clean_csv: str = "",
    open_after: bool = True,
) -> dict:
    """读文件 → 清洗 → 分析 → 写报告，返回一份摘要（命令行与界面共用）。"""
    columns, rows, display = read_table(path, sheet=sheet or None)
    cleaning = None
    if options is not None:
        result = clean_table(columns, rows, options)
        columns, rows = result.columns, result.rows
        cleaning = result.report

    payload = analyze_table(
        columns,
        rows,
        source=display,
        engine="python",
        preview_rows=40,
        title=None,
    )
    if cleaning:
        payload["cleaning"] = cleaning

    if not output:
        stem = os.path.splitext(os.path.basename(path))[0]
        output = os.path.join(os.path.dirname(os.path.abspath(path)), "%s_报告.html" % stem)

    written = write_report(payload, output)

    clean_path = ""
    clean_escaped = 0
    if clean_csv:
        clean_stats = {}
        clean_path = write_clean_csv(clean_csv, columns, rows, clean_stats)
        clean_escaped = clean_stats.get("escaped", 0)

    if open_after:
        import webbrowser

        webbrowser.open("file:///" + written.replace(os.sep, "/"))

    return {
        "report": written,
        "clean_csv": clean_path,
        "clean_escaped": clean_escaped,
        "payload": payload,
        "cleaning": cleaning,
    }


# --------------------------------------------------------------------------- #
# 图形界面
# --------------------------------------------------------------------------- #
def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("数据分析工具 · 本地版")
    root.geometry("620x430")
    root.minsize(560, 400)

    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)

    file_var = tk.StringVar()
    sheet_var = tk.StringVar()
    dedupe_var = tk.BooleanVar(value=True)
    trim_var = tk.BooleanVar(value=True)
    export_var = tk.BooleanVar(value=False)
    status_var = tk.StringVar(value="选择一个表格文件开始（" + SUPPORTED_HINT + "）")

    ttk.Label(frame, text="数据文件").grid(row=0, column=0, sticky="w", pady=(0, 6))
    entry = ttk.Entry(frame, textvariable=file_var)
    entry.grid(row=0, column=1, sticky="we", padx=8, pady=(0, 6))

    sheet_box = ttk.Combobox(frame, textvariable=sheet_var, state="readonly", values=())
    def pick_file() -> None:
        path = filedialog.askopenfilename(title="选择表格文件", filetypes=FILE_TYPES)
        if not path:
            return
        file_var.set(path)
        names = list_sheets(path)
        if len(names) > 1:
            sheet_box["values"] = names
            sheet_var.set(names[0])
            status_var.set("这个工作簿有 %d 个工作表，默认读第一个" % len(names))
        else:
            sheet_box["values"] = ()
            sheet_var.set("")
            status_var.set("已选择：" + os.path.basename(path))

    ttk.Button(frame, text="浏览…", command=pick_file).grid(row=0, column=2, pady=(0, 6))

    ttk.Label(frame, text="工作表").grid(row=1, column=0, sticky="w", pady=(0, 6))
    sheet_box.grid(row=1, column=1, sticky="we", padx=8, pady=(0, 6))

    box = ttk.LabelFrame(frame, text="清洗（默认只做无损动作）", padding=10)
    box.grid(row=2, column=0, columnspan=3, sticky="we", pady=10)
    box.columnconfigure(1, weight=1)

    ttk.Checkbutton(box, text="去掉单元格首尾空白", variable=trim_var).grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(box, text="删除完全重复的行", variable=dedupe_var).grid(row=0, column=1, sticky="w")

    ttk.Label(box, text="缺失值").grid(row=1, column=0, sticky="w", pady=(8, 0))
    fill_box = ttk.Combobox(box, state="readonly", values=[label for label, _ in FILL_LABELS])
    fill_box.current(0)
    fill_box.grid(row=1, column=1, sticky="we", pady=(8, 0))

    ttk.Label(box, text="异常值").grid(row=2, column=0, sticky="w", pady=(8, 0))
    outlier_box = ttk.Combobox(box, state="readonly", values=[label for label, _ in OUTLIER_LABELS])
    outlier_box.current(0)
    outlier_box.grid(row=2, column=1, sticky="we", pady=(8, 0))

    ttk.Checkbutton(box, text="同时导出清洗后的 CSV", variable=export_var).grid(
        row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

    bar = ttk.Frame(frame)
    bar.grid(row=3, column=0, columnspan=3, sticky="we", pady=(4, 10))

    def generate() -> None:
        path = file_var.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showwarning("还没选文件", "请先选择一个数据文件。")
            return
        fill_index = fill_box.current()
        outlier_index = outlier_box.current()
        options = CleanOptions(
            trim=trim_var.get(),
            dedupe=dedupe_var.get(),
            fill=FILL_LABELS[max(0, fill_index)][1],
            outliers=OUTLIER_LABELS[max(0, outlier_index)][1],
        )
        clean_csv = ""
        if export_var.get():
            stem = os.path.splitext(os.path.basename(path))[0]
            clean_csv = os.path.join(os.path.dirname(os.path.abspath(path)), "%s_清洗后.csv" % stem)
        status_var.set("正在处理…")
        root.update_idletasks()
        try:
            result = build_report(
                path,
                sheet=sheet_var.get() if sheet_var.get() in (sheet_box["values"] or ()) else "",
                options=options,
                clean_csv=clean_csv,
            )
        except Exception as error:  # noqa: BLE001 - 界面里必须把任何异常都变成可读提示
            status_var.set("处理失败：" + str(error))
            messagebox.showerror("处理失败", str(error) + "\n\n如果反复失败，可在终端运行以查看详细报错。")
            return
        meta = result["payload"]["meta"]
        status = "完成：%d 行 × %d 列，报告已生成并打开" % (meta["rows"], meta["cols"])
        if result.get("clean_escaped"):
            status += "；清洗结果里有 %d 格以 = + - @ 开头，已加单引号防公式执行" % result["clean_escaped"]
        status_var.set(status)

    ttk.Button(bar, text="生成报告", command=generate).pack(side="left")
    ttk.Label(bar, text="报告会生成在数据文件旁边，并用浏览器打开", foreground="#666").pack(
        side="left", padx=12)

    status = ttk.Label(frame, textvariable=status_var, foreground="#444", wraplength=560, justify="left")
    status.grid(row=4, column=0, columnspan=3, sticky="we")

    root.mainloop()
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv:
        try:
            return run_gui()
        except Exception:  # 没有 tkinter / 没有图形界面时退回命令行帮助
            print("当前环境无法打开图形窗口，改用命令行：")
            print("  python main.py 数据.xlsx -o 报告.html")
            print("  python main.py --help")
            traceback.print_exc()
            return 2

    if argv[0] in ("-h", "--help"):
        print(__doc__)
        print("可用参数：")
        print("  --sheet 名称或序号    指定 Excel 工作表")
        print("  --clean 路径          导出清洗后的 CSV")
        print("  --fill  方式          缺失处理：none/drop/mean/median/mode/ffill/value")
        print("  --outliers 方式       IQR 异常值：none/flag/drop")
        print("  -o 路径               报告输出位置")
        print("  --open                生成后打开报告")
        return 0

    # 有参数就是命令行模式：直接交给引擎，行为与 python -m glassdata 完全一致
    from glassdata.cli import main as engine_main

    return engine_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
