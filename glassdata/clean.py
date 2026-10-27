"""数据清洗：把「体检出来的问题」真的改掉，并且一步一步记账。

设计取舍：
1. 每一步都可单独关闭，默认只做「不会改变含义」的动作（去空白、去空行、去重）；
2. 每步都记一条日志：改了什么、动了多少格/多少行——不给「静默修改数据」；
3. 填补只填补显式缺失，不做插值外推；异常值默认只标记不删除；
4. 清洗前后各算一份行数 / 缺失 / 重复，报告里并排显示，用户能判断这份数据还能不能用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .core import _round, is_missing, parse_number

FILL_CHOICES = ("none", "drop", "mean", "median", "mode", "ffill", "value")
OUTLIER_CHOICES = ("none", "flag", "drop")

_FULLWIDTH_SPACE = "\u3000"
_SOFT_SPACE = "\u00a0"
# 已经是干净写法的数字：原样保留小数位数（180.50 不该被改成 180.5）
_PLAIN_NUMBER = re.compile(r"^[+-]?\d+(\.\d+)?$")


@dataclass
class CleanOptions:
    """清洗策略。默认值 = 只做无损动作。"""

    trim: bool = True
    drop_blank: bool = True
    dedupe: bool = True
    coerce: bool = True
    fill: str = "none"
    fill_value: str = ""
    outliers: str = "none"

    def normalized(self) -> "CleanOptions":
        fill = self.fill if self.fill in FILL_CHOICES else "none"
        outliers = self.outliers if self.outliers in OUTLIER_CHOICES else "none"
        return CleanOptions(
            trim=self.trim,
            drop_blank=self.drop_blank,
            dedupe=self.dedupe,
            coerce=self.coerce,
            fill=fill,
            fill_value=self.fill_value,
            outliers=outliers,
        )

    def summary(self) -> List[str]:
        """给用户看的一句话策略说明。"""
        parts: List[str] = []
        if self.trim:
            parts.append("去首尾空白")
        if self.drop_blank:
            parts.append("删空行")
        if self.dedupe:
            parts.append("去重")
        if self.coerce:
            parts.append("数字写法归一")
        if self.fill != "none":
            parts.append(
                {
                    "drop": "删除含缺失的行",
                    "mean": "均值填补",
                    "median": "中位数填补",
                    "mode": "众数填补",
                    "ffill": "前值填补",
                    "value": "固定值填补",
                }.get(self.fill, self.fill)
            )
        if self.outliers == "flag":
            parts.append("IQR 标记异常值")
        elif self.outliers == "drop":
            parts.append("IQR 剔除异常值")
        return parts


@dataclass
class CleanResult:
    columns: List[str]
    rows: List[List[str]]
    report: Dict[str, Any] = field(default_factory=dict)


def _blank_row(row: Sequence[str]) -> bool:
    return all(str(cell).strip() == "" for cell in row)


def _count_missing(rows: Sequence[Sequence[str]]) -> int:
    return sum(1 for row in rows for cell in row if is_missing(cell))


def _count_duplicates(rows: Sequence[Sequence[str]]) -> int:
    return len(rows) - len({tuple(str(cell) for cell in row) for row in rows})


def _stats(columns: Sequence[str], rows: Sequence[Sequence[str]]) -> Dict[str, int]:
    return {
        "rows": len(rows),
        "columns": len(columns),
        "missing": _count_missing(rows),
        "duplicates": _count_duplicates(rows),
    }


def _numeric_column(rows: Sequence[Sequence[str]], index: int) -> Tuple[bool, List[float]]:
    """该列是否够格当数值列处理，以及解析出来的数值。"""
    values: List[float] = []
    total = 0
    for row in rows:
        cell = row[index] if index < len(row) else ""
        if is_missing(cell):
            continue
        total += 1
        number = parse_number(cell)
        if number is not None:
            values.append(number)
    if not total:
        return False, []
    return len(values) / total >= 0.95, values


def _format_number(value: float) -> str:
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return text or "0"


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _mode(rows: Sequence[Sequence[str]], index: int) -> str:
    counter: Dict[str, int] = {}
    for row in rows:
        cell = row[index] if index < len(row) else ""
        if is_missing(cell):
            continue
        counter[cell] = counter.get(cell, 0) + 1
    if not counter:
        return ""
    return max(counter.items(), key=lambda item: (item[1], item[0]))[0]


def clean_table(
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    options: Optional[CleanOptions] = None,
) -> CleanResult:
    """按策略清洗表格，返回新表与「改了什么」的完整记录。"""
    options = (options or CleanOptions()).normalized()
    work: List[List[str]] = [[str(cell) for cell in row] for row in rows]
    columns_list = [str(name) for name in columns]
    before = _stats(columns_list, work)
    steps: List[Dict[str, Any]] = []

    # 1. 首尾空白（含全角空格、不换行空格）—— 最常见的隐形脏数据
    if options.trim:
        touched = 0
        for row in work:
            for index, cell in enumerate(row):
                cleaned = cell.replace(_SOFT_SPACE, " ").replace(_FULLWIDTH_SPACE, " ").strip()
                if cleaned != cell:
                    row[index] = cleaned
                    touched += 1
        steps.append({
            "name": "去空白",
            "detail": "去掉 %d 个单元格的首尾空白（含全角空格、不换行空格）" % touched if touched
            else "没有发现多余空白",
            "changed": touched,
        })

    # 2. 整行为空
    if options.drop_blank:
        kept = [row for row in work if not _blank_row(row)]
        removed = len(work) - len(kept)
        work = kept
        steps.append({
            "name": "删空行",
            "detail": "删除 %d 行整行为空的记录" % removed if removed else "没有整行为空的行",
            "changed": removed,
        })

    # 3. 完全重复行（保留第一次出现的那条）
    if options.dedupe:
        seen = set()
        kept = []
        for row in work:
            key = tuple(row)
            if key in seen:
                continue
            seen.add(key)
            kept.append(row)
        removed = len(work) - len(kept)
        work = kept
        steps.append({
            "name": "去重",
            "detail": "删除 %d 行完全重复的记录（保留首次出现）" % removed if removed else "没有完全重复的行",
            "changed": removed,
        })

    # 4. 数字写法归一：1,234 → 1234，¥99.00 → 99，(20) → -20。
    #    已经是干净写法的（180.50）原样保留——小数位数是业务约定，不是脏数据；
    #    百分比也不动，把 12% 改写成 0.12 虽然数值等价，但会让人以为数据被改过。
    if options.coerce:
        touched = 0
        for index in range(len(columns_list)):
            for row in work:
                cell = row[index] if index < len(row) else ""
                if is_missing(cell) or "%" in cell or _PLAIN_NUMBER.match(cell):
                    continue
                number = parse_number(cell)
                if number is None:
                    continue
                text = _format_number(number)
                if text != cell:
                    row[index] = text
                    touched += 1
        steps.append({
            "name": "数字归一",
            "detail": "把 %d 个单元格的数字写法统一（千分位、货币符号、会计负数）" % touched if touched
            else "数字写法已经统一",
            "changed": touched,
        })

    # 5. 缺失处理
    #    filled_cells 只统计「真的被填上了的格子」。
    #    不能拿 before.missing - after.missing 顶替：被删掉的空行里也有缺口，
    #    那些格子并没有被填补，只是随行一起没了，混进来会让人以为填补动了更多数据。
    filled_cells = 0
    if options.fill != "none":
        missing_before = _count_missing(work)
        if options.fill == "drop":
            kept = [row for row in work if not any(is_missing(cell) for cell in row)]
            removed = len(work) - len(kept)
            work = kept
            steps.append({
                "name": "缺失处理",
                "detail": "原有 %d 格缺失，删除含缺失的 %d 行后剩 %d 行" % (missing_before, removed, len(work)),
                "changed": removed,
            })
        else:
            filled = 0
            notes: List[str] = []
            for index, name in enumerate(columns_list):
                cells = [row[index] if index < len(row) else "" for row in work]
                holes = [position for position, cell in enumerate(cells) if is_missing(cell)]
                if not holes:
                    continue
                is_numeric, numbers = _numeric_column(work, index)
                replacement: Optional[str] = None
                label = ""
                if options.fill in ("mean", "median") and is_numeric and numbers:
                    value = sum(numbers) / len(numbers) if options.fill == "mean" else _median(numbers)
                    replacement = _format_number(_round(value, 6) or 0.0)
                    label = "均值" if options.fill == "mean" else "中位数"
                elif options.fill == "mode":
                    replacement = _mode(work, index)
                    label = "众数"
                elif options.fill == "value":
                    replacement = options.fill_value
                    label = "固定值"
                elif options.fill == "ffill":
                    last = ""
                    for row in work:
                        cell = row[index] if index < len(row) else ""
                        if is_missing(cell):
                            if last:
                                row[index] = last
                                filled += 1
                        else:
                            last = cell
                    if filled:
                        notes.append("%s 用前值填补" % name)
                    continue
                if replacement is None or replacement == "":
                    if options.fill in ("mean", "median") and not is_numeric:
                        # 不说 --fill mode：这段话在网页端与 HTML 报告里同样会显示，
                        # 命令行参数对那两个场景是噪音
                        notes.append("%s 是文本列，%d 格缺失未填补（可改用「众数」填补）"
                                     % (name, len(holes)))
                    continue
                for position in holes:
                    work[position][index] = replacement
                    filled += 1
                notes.append("%s 用%s %s 填补 %d 格" % (name, label, replacement, len(holes)))
            detail = "；".join(notes) if notes else "没有需要填补的缺失值"
            steps.append({"name": "缺失处理", "detail": detail, "changed": filled})
            filled_cells = filled

    # 6. 异常值：IQR 1.5 倍法则。默认只标记，不替你删数据。
    if options.outliers != "none":
        flagged: Dict[int, set] = {}
        notes: List[str] = []
        for index, name in enumerate(list(columns_list)):
            is_numeric, numbers = _numeric_column(work, index)
            if not is_numeric or len(numbers) < 8:
                continue
            ordered = sorted(numbers)
            q1 = ordered[int(len(ordered) * 0.25)]
            q3 = ordered[int(len(ordered) * 0.75)]
            span = q3 - q1
            if span <= 0:
                continue
            low, high = q1 - 1.5 * span, q3 + 1.5 * span
            hits = set()
            for position, row in enumerate(work):
                cell = row[index] if index < len(row) else ""
                if is_missing(cell):
                    continue
                number = parse_number(cell)
                if number is None:
                    continue
                if number < low or number > high:
                    hits.add(position)
            if hits:
                flagged[index] = hits
                notes.append("%s 有 %d 个超出 [%s, %s]" % (
                    name, len(hits),
                    _format_number(_round(low, 2) or 0.0), _format_number(_round(high, 2) or 0.0)))

        total = sum(len(positions) for positions in flagged.values())
        if options.outliers == "drop" and flagged:
            drop_positions = set().union(*flagged.values())
            work = [row for position, row in enumerate(work) if position not in drop_positions]
            notes.append("已删除 %d 行含极端值的记录" % len(drop_positions))
        elif options.outliers == "flag" and flagged:
            for index in sorted(flagged):
                columns_list.append(columns_list[index] + "_异常")
                for position, row in enumerate(work):
                    row.append("是" if position in flagged[index] else "否")
            notes.append("已追加「_异常」标记列（仅标记，不删除）")

        steps.append({
            "name": "异常值",
            "detail": "；".join(notes) if notes else "未发现 IQR 意义上的极端值",
            "changed": total,
        })

    after = _stats(columns_list, work)
    report = {
        "options": options.summary(),
        "steps": steps,
        "before": before,
        "after": after,
        "removed_rows": max(0, before["rows"] - after["rows"]),
        "filled_cells": filled_cells,
        "columns_added": after["columns"] - before["columns"],
    }
    return CleanResult(columns=columns_list, rows=work, report=report)


# --------------------------------------------------------------------------- #
# CSV 公式注入（CWE-1236）
#
# 导出的 CSV 是给 Excel 双击打开的。只要单元格以 = + - @ 或制表符/回车开头，
# Excel 就把它当公式执行——`=cmd|'/C calc'!A0` 会直接起一个计算器，
# `=HYPERLINK("http://…","点我")` 是钓鱼入口。这条路径不需要宏、不需要用户点「启用」。
#
# 取值本身是用户的数据，我们不改它的「含义」，只在**导出这一层**加一个单引号前缀：
# Excel 会把整格当文本，显示时连引号也不显示。真正要拦住的是「被当代码执行」。
#
# 纯数字必须豁免，否则 -1200、+5 这类正常负数会被写成文本，排序和求和全废。
# --------------------------------------------------------------------------- #
_CSV_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

# 「是不是数字」必须两端给出同一个答案，否则豁免规则会在某个角落分叉。
# 不用 float()：它接受下划线（1_0）和 inf/nan，而 JS 的 Number() 接受 0x 前缀，
# 两边天然不一致。用同一条正则，只认十进制写法。
_NUMBER_TEXT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def csv_neutralize(text: str) -> Optional[str]:
    """需要中和时返回加前缀后的写法，否则返回 None。

    前端 clean.js 的 csvNeutralize 与本函数逐字对应，由差分测试盯着。
    """
    if not text or text[0] not in _CSV_TRIGGERS:
        return None
    if _NUMBER_TEXT.match(text):
        return None
    return "'" + text


def is_number_text(text: str) -> bool:
    """是不是一个能安全交给 Excel 的数字。"""
    return bool(text) and _NUMBER_TEXT.match(text) is not None


def write_clean_csv(
    path: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    stats: Optional[Dict[str, Any]] = None,
) -> str:
    """导出清洗后的表。utf-8-sig：Excel 双击打开不乱码。

    stats 是出参：`stats["escaped"]` 返回被加了单引号前缀的单元格个数，
    调用方据此提示用户——改了就得说，不能静默。
    """
    import csv
    import os

    escaped = 0

    def safe(value: Any) -> str:
        nonlocal escaped
        text = "" if value is None else str(value)
        guarded = csv_neutralize(text)
        if guarded is None:
            return text
        escaped += 1
        return guarded

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([safe(cell) for cell in columns])
        writer.writerows([[safe(cell) for cell in row] for row in rows])
    if stats is not None:
        stats["escaped"] = escaped
        stats["rows"] = len(rows)
    return os.path.abspath(path)
