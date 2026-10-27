"""数据分析引擎（仅使用 Python 标准库）。

设计要点：
1. 不依赖 pandas / numpy，任何装了 Python 3.9+ 的机器都能直接跑；
2. 所有推断都基于数据本身（占比阈值 95%），不做语义猜测；
3. 输出是一份纯 JSON 可序列化的 payload，前端与 CLI 共用同一套结构。
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

MISSING_TOKENS = frozenset(
    {"", "na", "n/a", "nan", "null", "none", "nil", "-", "--", "—", "?", "#n/a"}
)
TRUE_TOKENS = frozenset({"true", "yes", "y", "t", "是", "对", "真"})
FALSE_TOKENS = frozenset({"false", "no", "n", "f", "否", "错", "假"})

_DATE_FORMATS: Tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y年%m月%d日",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m",
    "%Y/%m",
    "%Y年%m月",
    "%Y%m%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
)

_ISO_TZ = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$"
)

# 快速路径：先用一次正则判定形态，再直接构造 datetime。
# 逐个 strptime 试格式的代价极高（strptime 每次都要编译模式），
# 在 5 万行数据上实测占整个分析耗时的 82%，故改为形态优先匹配。
_TIME_TAIL = r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d{1,9})?)?"
_DATE_DASH = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})" + _TIME_TAIL + r"$")
_DATE_SLASH = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})" + _TIME_TAIL + r"$")
_DATE_DOT = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})" + _TIME_TAIL + r"$")
_DATE_CN = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日" + _TIME_TAIL + r"$")
_DATE_YEAR_MONTH = re.compile(r"^(\d{4})[-/.](\d{1,2})$")
_DATE_CN_MONTH = re.compile(r"^(\d{4})年(\d{1,2})月$")
_DATE_COMPACT = re.compile(r"^(\d{8})$")
_DATE_DAY_FIRST = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")
# 兜底路径的前置筛子：所有目标格式都以数字开头且含 4 位连续数字。
# 先用它挡掉「文本 / 布尔」类取值，避免无谓地试 16 次 strptime。
_DATE_DIGIT_RUN = re.compile(r"\d{4}")

# 类型判定阈值：某一类型可解析比例达到该值，即认定整列属于该类型
_TYPE_THRESHOLD = 0.95
# 相关性矩阵最多展示的数值列数量
_MAX_CORR_COLUMNS = 10
# 类别列展示的 Top N
_TOP_N = 12
# 日期书写顺序：dmy = 日/月/年，mdy = 月/日/年。
# 像 01/05/2026 这种两段都 ≤ 12 的写法本身是歧义的，引擎不替用户猜，
# 而是按声明顺序解析并在 payload 里挂出告警，由使用者确认后切换。
DATE_ORDERS: Tuple[str, ...] = ("dmy", "mdy")
_DEFAULT_DATE_ORDER = "dmy"


# --------------------------------------------------------------------------- #
# 基础解析
# --------------------------------------------------------------------------- #
def is_missing(value: Any) -> bool:
    """判断单元格是否应视为缺失值。"""
    if value is None:
        return True
    return str(value).strip().lower() in MISSING_TOKENS


def parse_number(value: Any) -> Optional[float]:
    """把单元格解析为浮点数；支持千分位、货币符号、百分号、会计负数。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # 首字符不是数字/符号的（汉字、字母开头）不可能是数字。
    # 直接返回，省掉后面的清洗和一次必然失败的 float()——
    # 文本列上百万行，异常构造本身就是可观的开销。
    head = text[0]
    if not (head.isdigit() or head in "+-.(¥￥$€£"):
        return None
    # 快路径：绝大多数格子就是干干净净的数字
    try:
        plain = float(text)
    except ValueError:
        pass
    else:
        return plain if math.isfinite(plain) else None
    percent = text.endswith("%")
    if percent:
        text = text[:-1]
    text = text.replace(",", "").replace("_", "").replace(" ", "")
    for symbol in ("¥", "￥", "$", "€", "£"):
        text = text.replace(symbol, "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        number = float(text)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return number / 100.0 if percent else number


def _from_ymd(match: Any) -> Optional[datetime]:
    """从形如 (年, 月, 日, [时, 分, 秒]) 的匹配结果直接构造 datetime。"""
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    hour = int(match.group(4) or 0)
    minute = int(match.group(5) or 0)
    second = int(match.group(6) or 0)
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def _resolve_date_order(order: Optional[str]) -> str:
    """把外部传入的日期顺序规范化；非法取值回落到默认值。"""
    if order in DATE_ORDERS:
        return str(order)
    return _DEFAULT_DATE_ORDER


def parse_date(value: Any, order: str = _DEFAULT_DATE_ORDER) -> Optional[datetime]:
    """把单元格解析为 datetime；失败返回 None。

    `order` 只影响「两段都可能是月份」的写法（如 01/05/2026）：
    dmy 视为 日/月/年，mdy 视为 月/日/年。当声明顺序构造出的日期非法时
    （如 25/12/2026 在 mdy 下没有 25 月），自动退到另一种解释，
    避免整列因为顺序选错而被打成文本。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not ("0" <= text[0] <= "9") or not _DATE_DIGIT_RUN.search(text):
        return None

    for pattern in (_DATE_DASH, _DATE_SLASH, _DATE_DOT, _DATE_CN):
        match = pattern.match(text)
        if match:
            moment = _from_ymd(match)
            if moment is not None:
                return moment

    match = _DATE_YEAR_MONTH.match(text) or _DATE_CN_MONTH.match(text)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), 1)
        except ValueError:
            return None

    if _DATE_COMPACT.match(text):
        try:
            return datetime(int(text[0:4]), int(text[4:6]), int(text[6:8]))
        except ValueError:
            return None

    match = _DATE_DAY_FIRST.match(text)
    if match:
        first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        day_first = _resolve_date_order(order) == "dmy"
        # 按声明顺序优先，非法时退到另一种解释
        attempts = ((first, second), (second, first)) if day_first else ((second, first), (first, second))
        for day, month in attempts:
            try:
                return datetime(year, month, day)
            except ValueError:
                continue
        return None

    match = _ISO_TZ.match(text)
    if match:
        clock = match.group(2).split(":")
        try:
            return datetime(
                int(match.group(1)[0:4]),
                int(match.group(1)[5:7]),
                int(match.group(1)[8:10]),
                int(clock[0]),
                int(clock[1]),
                int(clock[2]) if len(clock) > 2 else 0,
            )
        except ValueError:
            return None

    candidate = text.replace("T", " ")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    return None


def _ambiguous_once(value: Any) -> bool:
    """单个取值在「日/月/年」与「月/日/年」下是否都成立。"""
    match = _DATE_DAY_FIRST.match(str(value).strip())
    if not match:
        return False
    first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if first == second or first > 12 or second > 12:
        return False
    try:
        datetime(year, second, first)
        datetime(year, first, second)
    except ValueError:
        return False
    return True


def date_order_ambiguity(values: Sequence[Any]) -> int:
    """统计有多少个取值在「日/月/年」与「月/日/年」下都成立。

    只有两段都 ≤ 12 且取值不同时才算真歧义；像 25/12/2026 只可能有一种解释。
    引擎不会替用户挑一个偷偷用掉——这个计数会挂到列画像上，由使用者确认。

    按「不同取值 + 出现次数」统计：日期列往往只有几十个不同值却重复上万次，
    逐个正则太浪费，先归并再判断。
    """
    tally: Dict[Any, int] = {}
    for value in values:
        if is_missing(value):
            continue
        key = value if isinstance(value, (str, int, float, bool)) else str(value)
        tally[key] = tally.get(key, 0) + 1
    return sum(repeats for value, repeats in tally.items() if _ambiguous_once(value))


def parse_bool(value: Any) -> Optional[bool]:
    """识别 true/false 类取值，不吞掉 0/1（保留其数值语义）。"""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in TRUE_TOKENS:
        return True
    if text in FALSE_TOKENS:
        return False
    return None


# --------------------------------------------------------------------------- #
# 统计工具
# --------------------------------------------------------------------------- #
def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None:
        return None
    if not math.isfinite(value):
        return None
    return round(value, digits)


def _quantile(sorted_values: Sequence[float], q: float) -> Optional[float]:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return sorted_values[low]
    share = position - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * share


def _short(number: float) -> str:
    """把数值压成适合坐标轴的短标签。"""
    magnitude = abs(number)
    if magnitude >= 1e8:
        return f"{number / 1e8:.2f}亿"
    if magnitude >= 1e4:
        return f"{number / 1e4:.1f}万"
    if magnitude >= 1000:
        return f"{number:.0f}"
    if magnitude >= 10:
        return f"{number:.1f}".rstrip("0").rstrip(".")
    if magnitude >= 1:
        return f"{number:.2f}".rstrip("0").rstrip(".")
    if number == 0:
        return "0"
    return f"{number:.3g}"


def _bin_count(length: int) -> int:
    return max(8, min(24, int(round(math.sqrt(max(length, 1)) * 1.6))))


def _numeric_hist(values: Sequence[float]) -> Dict[str, Any]:
    low = min(values)
    high = max(values)
    if high <= low:
        return {"labels": [_short(low)], "counts": [len(values)], "edges": [low, high]}
    bins = _bin_count(len(values))
    width = (high - low) / bins
    counts = [0] * bins
    for value in values:
        index = int((value - low) / width)
        if index >= bins:
            index = bins - 1
        elif index < 0:
            index = 0
        counts[index] += 1
    edges = [low + width * i for i in range(bins + 1)]
    labels = [_short(edges[i]) for i in range(bins)]
    return {"labels": labels, "counts": counts, "edges": [_round(e, 6) for e in edges]}


def _discrete_hist(values: Sequence[float]) -> Dict[str, Any]:
    """离散整数列按取值计数：避免出现 2.38 这类只为分箱服务的边界值。"""
    tally: Dict[int, int] = {}
    for value in values:
        key = int(value)
        tally[key] = tally.get(key, 0) + 1
    keys = sorted(tally)
    return {
        "labels": [str(key) for key in keys],
        "counts": [tally[key] for key in keys],
        "edges": [key - 0.5 for key in keys] + [keys[-1] + 0.5],
        "discrete": True,
    }


def _datetime_hist(dates: Sequence[datetime]) -> Dict[str, Any]:
    """按时间分箱。用「相对首条记录的天数」做坐标，
    避免 timestamp() 引入本地时区依赖——同一份数据在任何时区机器上结果一致。
    """
    base = min(dates)
    latest = max(dates)
    offsets = [(moment - base).total_seconds() for moment in dates]
    span_days = (latest - base).total_seconds() / 86400.0
    if not span_days:
        return {
            "labels": [base.strftime("%Y-%m-%d")],
            "counts": [len(dates)],
            "edges": [0.0, 1.0],
        }
    bins = _bin_count(len(dates))
    width = span_days * 86400.0 / bins
    counts = [0] * bins
    for offset in offsets:
        index = int(offset / width)
        if index >= bins:
            index = bins - 1
        elif index < 0:
            index = 0
        counts[index] += 1
    if span_days > 540:
        pattern = "%Y-%m"
    elif span_days > 45:
        pattern = "%m-%d"
    else:
        pattern = "%m-%d %H:%M"
    labels = [
        (base + timedelta(seconds=width * i)).strftime(pattern) for i in range(bins)
    ]
    edges = [round(width * i, 3) for i in range(bins + 1)]
    return {"labels": labels, "counts": counts, "edges": edges}


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    length = len(xs)
    if length < 3 or length != len(ys):
        return None
    means_x = sum(xs) / length
    means_y = sum(ys) / length
    sxx = sum((x - means_x) ** 2 for x in xs)
    syy = sum((y - means_y) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - means_x) * (y - means_y) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


# --------------------------------------------------------------------------- #
# 单列画像
# --------------------------------------------------------------------------- #
def _profile_column(
    name: str, raw: Sequence[Any], date_order: str = _DEFAULT_DATE_ORDER
) -> Tuple[Dict[str, Any], Optional[Dict[int, float]]]:
    total = len(raw)
    # 保留原始行号：跨列计算相关性时必须用同一行号对齐，
    # 否则各列缺失位置不同会导致错位匹配。
    indexed = [(index, value) for index, value in enumerate(raw) if not is_missing(value)]
    values = [value for _, value in indexed]
    missing = total - len(values)
    count = len(values)
    profile: Dict[str, Any] = {
        "name": name,
        "type": "empty",
        "total": total,
        "missing": missing,
        "unique": 0,
        "stats": {},
        "hist": None,
        "top": [],
        "samples": [str(value)[:120] for value in values[:6]],
    }
    if count == 0:
        return profile, None

    unique_values = {str(value) for value in values}
    profile["unique"] = len(unique_values)

    numbers = [(index, parse_number(value)) for index, value in indexed]
    numeric_ok = sum(1 for _, number in numbers if number is not None)
    if numeric_ok / count >= _TYPE_THRESHOLD:
        numeric_values = [number for _, number in numbers if number is not None]
        ordered = sorted(numeric_values)
        size = len(numeric_values)
        total = sum(numeric_values)
        mean = total / size
        # 注意：均值必须先在循环外算好。此前把 sum(...) 写在生成器内部，
        # 导致每个元素都要重新求和一次整列，复杂度退化为 O(n²)。
        if size > 1:
            variance = sum((value - mean) ** 2 for value in numeric_values) / (size - 1)
            std = math.sqrt(variance) if variance > 0 else 0.0
        else:
            std = 0.0
        profile["type"] = "number"
        profile["stats"] = {
            "min": _round(ordered[0]),
            "max": _round(ordered[-1]),
            "mean": _round(mean),
            "median": _round(_quantile(ordered, 0.5)),
            "q1": _round(_quantile(ordered, 0.25)),
            "q3": _round(_quantile(ordered, 0.75)),
            "sum": _round(total),
            "std": _round(std),
        }
        discrete = len(unique_values) <= 24 and all(
            float(value).is_integer() for value in numeric_values
        )
        profile["hist"] = (
            _discrete_hist(numeric_values) if discrete else _numeric_hist(numeric_values)
        )
        if len(unique_values) <= 20:
            counter = _counter(values)
            profile["top"] = [
                {"value": label, "count": amount}
                for label, amount in counter[:8]
            ]
        series = {
            index: number for index, number in numbers if number is not None
        }
        return profile, series

    # 日期解析要挨个试 4 种分隔符写法，是整条链路里最贵的一步。
    # 日期列的取值往往高度重复（一天几十行很常见），按取值缓存后
    # 20 万行 81 个不同日期只需解析 81 次。
    parsed_dates: Dict[Any, Optional[datetime]] = {}
    dates: List[Optional[datetime]] = []
    for value in values:
        key = value if isinstance(value, (str, int, float, bool)) else str(value)
        if key in parsed_dates:
            dates.append(parsed_dates[key])
        else:
            moment = parse_date(value, date_order)
            parsed_dates[key] = moment
            dates.append(moment)
    date_ok = sum(1 for moment in dates if moment is not None)
    if date_ok / count >= _TYPE_THRESHOLD:
        moments = [moment for moment in dates if moment is not None]
        # 只用到最早和最晚，不必给 20 万个时间点排序
        earliest = min(moments)
        latest = max(moments)
        profile["type"] = "datetime"
        profile["stats"] = {
            "min": earliest.strftime("%Y-%m-%d %H:%M"),
            "max": latest.strftime("%Y-%m-%d %H:%M"),
            "spanDays": _round((latest - earliest).total_seconds() / 86400.0, 2),
            "count": len(moments),
        }
        ambiguous = date_order_ambiguity(values)
        if ambiguous:
            profile["note"] = {
                "kind": "date-order",
                "ambiguous": ambiguous,
                "order": date_order,
            }
        profile["hist"] = _datetime_hist(moments)
        return profile, None

    bools = [parse_bool(value) for value in values]
    bool_ok = sum(1 for flag in bools if flag is not None)
    if bool_ok / count >= _TYPE_THRESHOLD:
        truthy = sum(1 for flag in bools if flag is True)
        profile["type"] = "boolean"
        profile["stats"] = {
            "trueCount": truthy,
            "falseCount": len(values) - truthy,
            "trueRate": _round(truthy / len(values), 4),
        }
        profile["top"] = [
            {"value": "true", "count": truthy},
            {"value": "false", "count": len(values) - truthy},
        ]
        return profile, None

    # 全列取值互不相同且行数足够：判定为标识列，避免生成无意义的分布图
    if count >= 8 and len(unique_values) == count:
        profile["type"] = "id"
        profile["stats"] = {"unique": count, "fullyUnique": True}
        return profile, None

    if len(unique_values) <= 40 or len(unique_values) / count <= 0.4:
        profile["type"] = "categorical"
        counter = _counter(values)
        profile["stats"] = {
            "topShare": _round(counter[0][1] / count, 4) if counter else None,
        }
        profile["top"] = [
            {"value": label, "count": amount} for label, amount in counter[:_TOP_N]
        ]
        return profile, None

    lengths = [len(str(value)) for value in values]
    profile["type"] = "text"
    profile["stats"] = {
        "avgLength": _round(sum(lengths) / len(lengths), 1),
        "maxLength": max(lengths),
    }
    return profile, None


def _counter(values: Sequence[Any]) -> List[Tuple[str, int]]:
    tally: Dict[str, int] = {}
    for value in values:
        key = str(value)
        tally[key] = tally.get(key, 0) + 1
    return sorted(tally.items(), key=lambda item: (-item[1], item[0]))


# --------------------------------------------------------------------------- #
# 汇总分析
# --------------------------------------------------------------------------- #
def _insights(
    profiles: Sequence[Dict[str, Any]],
    labels: Sequence[str],
    matrix: Sequence[Sequence[Optional[float]]],
    rows: int,
    missing_total: int,
) -> List[Dict[str, str]]:
    insights: List[Dict[str, str]] = []
    best: Optional[Tuple[float, str, str]] = None
    for i, left in enumerate(labels):
        for j, right in enumerate(labels):
            if j <= i:
                continue
            value = matrix[i][j]
            if value is None:
                continue
            if best is None or abs(value) > abs(best[0]):
                best = (value, left, right)
    if best and abs(best[0]) >= 0.5:
        direction = "正相关" if best[0] > 0 else "负相关"
        insights.append(
            {
                "kind": "corr",
                "text": f"最强线性关系：{best[1]} 与 {best[2]} 呈{direction}（皮尔逊系数 {best[0]:.2f}）",
            }
        )

    if rows:
        ranked = sorted(
            (profile for profile in profiles if profile["missing"] > 0),
            key=lambda profile: -profile["missing"] / max(profile["total"], 1),
        )
        if ranked:
            head = ranked[0]
            rate = head["missing"] / max(head["total"], 1)
            insights.append(
                {
                    "kind": "missing",
                    "text": f"缺失最集中的列是 {head['name']}，缺失 {head['missing']} 格（{rate * 100:.1f}%）",
                }
            )
        if missing_total == 0:
            insights.append({"kind": "missing", "text": "全部单元格均有值，未检测到缺失。"})

    categoricals = [profile for profile in profiles if profile["type"] == "categorical"]
    if categoricals:
        target = max(
            categoricals,
            key=lambda profile: profile["stats"].get("topShare") or 0.0,
        )
        share = target["stats"].get("topShare") or 0.0
        if target["top"]:
            insights.append(
                {
                    "kind": "category",
                    "text": f"{target['name']} 的取值最集中：{target['top'][0]['value']} 占 {share * 100:.1f}%",
                }
            )

    numeric = [profile["name"] for profile in profiles if profile["type"] == "number"]
    if numeric:
        insights.append(
            {
                "kind": "shape",
                "text": f"共识别出 {len(numeric)} 个数值列，可参与相关性计算与趋势对比。",
            }
        )
    return insights


def _collect_warnings(
    profiles: Sequence[Dict[str, Any]], row_count: int
) -> List[Dict[str, Any]]:
    """把各列画像里的可疑之处汇总成一份清单。

    这些提示的共同前提是：它们**不会让程序报错**，只会让报告看起来正常但结论偏了。
    对面向公众的工具来说，静默错误比崩溃更危险，所以宁可多说一句。
    """
    warnings: List[Dict[str, Any]] = []
    order_label = {"dmy": "日/月", "mdy": "月/日"}

    for profile in profiles:
        name = profile.get("name", "")
        note = profile.get("note")
        if isinstance(note, dict) and note.get("kind") == "date-order":
            current = order_label.get(str(note.get("order")), "日/月")
            other = "月/日" if current == "日/月" else "日/月"
            warnings.append(
                {
                    "level": "warn",
                    "kind": "date-order",
                    "column": name,
                    "ambiguous": note.get("ambiguous", 0),
                    "order": note.get("order"),
                    "message": (
                        f"「{name}」有 {note.get('ambiguous', 0)} 个日期两种读法都成立，"
                        f"当前按 {current} 解释；若数据实际是 {other}，最值会整体偏移"
                    ),
                }
            )

        if profile.get("type") == "empty":
            warnings.append(
                {
                    "level": "info",
                    "kind": "empty-column",
                    "column": name,
                    "message": f"「{name}」整列为空，已跳过画像",
                }
            )
        elif profile.get("type") != "empty" and profile.get("unique") == 1 and row_count > 1:
            warnings.append(
                {
                    "level": "info",
                    "kind": "constant-column",
                    "column": name,
                    "message": f"「{name}」只有一个取值，无法参与相关性分析",
                }
            )
        elif row_count > 0:
            missing = int(profile.get("missing", 0))
            if missing and missing / row_count >= 0.4:
                rate = round(missing / row_count * 100, 1)
                warnings.append(
                    {
                        "level": "warn",
                        "kind": "high-missing",
                        "column": name,
                        "message": f"「{name}」缺失率 {rate}%，基于该列的结论需谨慎",
                    }
                )
    return warnings


def analyze_table(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    source: str = "dataset",
    engine: str = "python",
    preview_rows: int = 40,
    title: Optional[str] = None,
    date_order: str = _DEFAULT_DATE_ORDER,
) -> Dict[str, Any]:
    """对表格做完整画像，返回可直接 json.dumps 的 payload。"""
    columns = list(columns)
    rows = [list(row) for row in rows]
    row_count = len(rows)
    column_count = len(columns)
    date_order = _resolve_date_order(date_order)

    profiles: List[Dict[str, Any]] = []
    series_map: Dict[str, Dict[int, float]] = {}
    missing_total = 0

    for index, name in enumerate(columns):
        raw = [row[index] if index < len(row) else "" for row in rows]
        profile, series = _profile_column(name, raw, date_order)
        missing_total += profile["missing"]
        profiles.append(profile)
        if series is not None:
            series_map[name] = series

    labels = list(series_map.keys())[:_MAX_CORR_COLUMNS]
    size = len(labels)
    # 相关系数矩阵是对称的，只算上半三角再镜像过去：
    # 6 列从 36 次成对计算降到 21 次，其中对角线还可以直接跳过。
    matrix: List[List[Optional[float]]] = [[None] * size for _ in range(size)]
    for row_index in range(size):
        left = labels[row_index]
        left_series = series_map[left]
        for column_index in range(row_index, size):
            right = labels[column_index]
            right_series = series_map[right]
            if row_index == column_index:
                # 自己和自己：点数不足 3 或整列为常数时 _pearson 会返回 None，
                # 其余情况按定义恒等于 1，不必真算一遍。
                column_values = list(left_series.values())
                if len(column_values) < 3:
                    matrix[row_index][column_index] = None
                else:
                    first = column_values[0]
                    matrix[row_index][column_index] = (
                        1.0 if any(item != first for item in column_values) else None
                    )
                continue
            # 取交集不要用 set(...) & set(...)：那会把两列各 20 万个行号
            # 先搬进集合再求交。沿着较短的那一列走，用 dict 的 O(1) 查另一列即可，
            # 行序仍是升序，与逐对独立计算的结果逐位一致。
            xs: List[float] = []
            ys: List[float] = []
            if len(left_series) <= len(right_series):
                for index, value in left_series.items():
                    mate = right_series.get(index)
                    if mate is None:
                        continue
                    xs.append(value)
                    ys.append(mate)
            else:
                for index, value in right_series.items():
                    mate = left_series.get(index)
                    if mate is None:
                        continue
                    xs.append(mate)
                    ys.append(value)
            if not xs:
                value = None
            else:
                coefficient = _pearson(xs, ys)
                value = None if coefficient is None else round(coefficient, 4)
            matrix[row_index][column_index] = value
            matrix[column_index][row_index] = value

    duplicate_rows = row_count - len({tuple(str(cell) for cell in row) for row in rows})
    cells = row_count * column_count
    completeness = 1.0 if cells == 0 else 1.0 - missing_total / cells
    warnings = _collect_warnings(profiles, row_count)

    payload: Dict[str, Any] = {
        "meta": {
            "title": title or f"{os.path.basename(str(source))} · 数据分析",
            "source": os.path.basename(str(source)),
            "engine": engine,
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "rows": row_count,
            "cols": column_count,
            "dateOrder": date_order,
        },
        "quality": {
            "missingTotal": missing_total,
            "cells": cells,
            "completeness": round(completeness, 4),
            "duplicateRows": duplicate_rows,
            "warnings": len(warnings),
        },
        "warnings": warnings,
        "columns": profiles,
        "correlation": {"labels": labels, "matrix": matrix},
        "insights": _insights(profiles, labels, matrix, row_count, missing_total),
        "preview": {
            "columns": columns,
            "rows": [
                ["" if cell is None else str(cell) for cell in row]
                for row in rows[: max(1, int(preview_rows))]
            ],
            "total": row_count,
        },
    }
    return payload


# --------------------------------------------------------------------------- #
# 读取文件
# --------------------------------------------------------------------------- #
# 带 BOM 的编码必须优先判定：GB18030 几乎能"成功"解出任何字节序列，
# 若先让它试，UTF-16 的字节会被解成一堆含换行的乱码（Excel 的「Unicode 文本」就是这样），
# 既不报错也不提示，用户只会看到表头变成乱码。
_BOM_ENCODINGS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32"),      # 必须排在 utf-16 之前
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),              # 编解码器自己认 BOM 并去掉
    (b"\xfe\xff", "utf-16"),
)

# 无 BOM 的 UTF-16 靠 NUL 字节分布识别：ASCII 文本在 UTF-16 里每隔一字节就是 0x00
_NUL_PROBE_BYTES = 4096


def _sniff_utf16(raw: bytes) -> Optional[str]:
    head = raw[:_NUL_PROBE_BYTES]
    if b"\x00" not in head:
        return None
    even_zeros = head[0::2].count(0)
    odd_zeros = head[1::2].count(0)
    half = max(1, len(head) // 2)
    if odd_zeros > half * 0.4 and even_zeros < half * 0.05:
        return "utf-16-le"
    if even_zeros > half * 0.4 and odd_zeros < half * 0.05:
        return "utf-16-be"
    return None


def _score_text(text: str) -> int:
    """给解码结果打分，用来在「都能解开」的编码之间选像样的那个。

    GB18030 与 Big5 的字节区间高度重叠，两边都能"成功"解开同一串字节，
    但错的那个会产出私用区（U+E000–U+F8FF）、罕用扩展区字符——
    这正是中文文档里不会出现、却是错解码典型产物的一类字符。
    """
    score = 0
    for ch in text[:4000]:
        cp = ord(ch)
        if cp < 0x80:
            score += 1                       # ASCII：两种解码都一样，不构成区分度
        elif 0x4E00 <= cp <= 0x9FFF:
            score += 2                       # 常用汉字
        elif 0x3000 <= cp <= 0x303F:
            score += 2                       # 中文标点
        elif 0xFF00 <= cp <= 0xFFEF:
            score += 2                       # 全角字符
        elif 0xE000 <= cp <= 0xF8FF:
            score -= 6                       # 私用区：几乎必然是错解码
        elif cp >= 0x20000:
            score -= 4                       # 罕用扩展区
        else:
            score -= 1
    return score


def _decode(raw: bytes) -> str:
    for bom, encoding in _BOM_ENCODINGS:
        if raw.startswith(bom):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                break
    guessed = _sniff_utf16(raw)
    if guessed:
        try:
            return raw.decode(guessed)
        except UnicodeDecodeError:
            pass

    # UTF-8 能解开就直接用，它不会误判（其余多字节编码极少能通过 UTF-8 的校验）
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass

    # 东亚多字节编码之间用打分比较，取更"像中文"的那个
    candidates = []
    for encoding in ("gb18030", "big5"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        candidates.append((_score_text(text), text))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    try:
        return raw.decode("latin-1")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _unique_headers(headers: Sequence[str]) -> List[str]:
    seen: Dict[str, int] = {}
    result: List[str] = []
    for index, header in enumerate(headers):
        name = header.strip() or f"col_{index + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        result.append(name)
    return result


# Excel（xlsx/xlsm/ods 都是 zip 容器）与旧版 xls（OLE 复合文档）的签名。
# 它们按文本读出来只是一堆乱码，最后会报"文件只有表头"这类误导性错误。
_ZIP_SIGNATURE = b"PK\x03\x04"
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_EXCEL_EXTENSIONS = (".xlsx", ".xlsm", ".xls", ".ods", ".numbers")

# 每行一个 JSON 的变体。扩展名必须显式列出来：它们按分隔文本解析会得到
# 形如 ['{"a":1', 'b:2}'] 的垃圾列名，既不报错也没法用。
_JSONL_EXTENSIONS = (".jsonl", ".ndjson")

# 二进制格式靠文件头识别，而不是靠扩展名——用户常把 parquet 改名成 .csv，
# 只认扩展名拦不住。每条都给出「下一步该干什么」，光说"不支持"等于没说。
_BINARY_SIGNATURES = (
    (_ZIP_SIGNATURE, "Excel / ODS 等压缩容器格式", "请用 Excel / WPS 打开后「另存为 CSV」"),
    (_OLE_SIGNATURE, "旧版 Excel（.xls）或 Access 数据库", "请另存为 CSV 后再导入"),
    (b"PAR1", "Parquet 列式文件", "请用 pandas 读取后 to_csv(..., encoding='utf-8-sig') 导出"),
    (b"SQLite format 3\x00", "SQLite 数据库", "请用 sqlite3 -header -csv 导出 CSV"),
    (b"<stata_dta>", "Stata 数据文件（.dta）", "请用 pandas.read_stata 后导出 CSV"),
    (b"$FL2", "SPSS 数据文件（.sav）", "请用 pandas.read_spss 后导出 CSV"),
    (b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xc2\xea\x81\x60", "SAS 数据文件（.sas7bdat）",
     "请用 pandas.read_sas 后导出 CSV"),
    (b"\x89HDF\r\n\x1a\n", "HDF5 文件", "请用 h5py / pandas 读取后导出 CSV"),
    (b"\x1f\x8b", "gzip 压缩文件", "请先解压（gunzip）再导入"),
    (b"%PDF", "PDF 文件", "不支持直接读取，请把表格另存为 CSV"),
    (b"\x89PNG\r\n\x1a\n", "PNG 图片", "不支持图片，请先导出为 CSV"),
    (b"\xff\xd8\xff", "JPEG 图片", "不支持图片，请先导出为 CSV"),
)


def _detect_binary(raw: bytes) -> Optional[str]:
    """命中已知二进制签名时返回中文说明，否则返回 None。"""
    for signature, name, advice in _BINARY_SIGNATURES:
        if raw.startswith(signature):
            return f"这看起来是{name}，不是文本表格：{advice}"
    return None


def _looks_binary(text: str) -> bool:
    """未知格式的兜底：正常表格不会有大量不可打印字符。

    没有这层判断，任何二进制文件都会先被 latin-1「成功」解码，
    再以「文件只有表头，没有数据行」告终——用户只会以为自己文件坏了。
    """
    sample = text[:4096]
    if not sample:
        return False
    control = sum(
        1 for ch in sample
        if (ord(ch) < 0x20 and ch not in "\t\n\r\f\v") or ord(ch) == 0x7F
    )
    return control >= 5 and control / len(sample) > 0.02


def _table_from_records(records: Sequence[object]) -> Tuple[List[str], List[List[str]]]:
    """把「记录列表」转成列名 + 行，JSON 与 JSONL 共用。

    对象数组取所有键的并集（后面的记录可能多出新键）；
    二维数组按最宽的那行动态补空，避免行长短不一把下游统计带崩。
    """
    if not records:
        raise ValueError("JSON 里没有记录：需要非空的对象数组")

    if isinstance(records[0], dict):
        columns: List[str] = []
        for record in records:
            if isinstance(record, dict):
                for key in record.keys():
                    if key not in columns:
                        columns.append(key)
        rows = [
            [_cell(record.get(column)) for column in columns] if isinstance(record, dict)
            else ["" for _ in columns]
            for record in records
        ]
        return columns, rows

    rows: List[List[str]] = []
    for record in records:
        if isinstance(record, (list, tuple)):
            rows.append([_cell(value) for value in record])
        else:
            rows.append([_cell(record)])
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    return [f"col_{index + 1}" for index in range(width)], padded


def read_table(
    path: str, sheet: Optional[str] = None, notes: Optional[List[str]] = None
) -> Tuple[List[str], List[List[str]], str]:
    """读取表格文件，返回 (列名, 行数据, 显示名)。

    .xlsx / .xlsm / .ods 由 excel 模块用标准库直接解析，sheet 可指定工作表
    （名称或从 1 开始的序号），不传则用第一张「能分析」的工作表——封面页会被跳过。
    notes 可选：读取过程中的提示（跳过标题行、自动换表）会追加进去。
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到文件：{path}")

    with open(path, "rb") as handle:
        raw = handle.read()

    extension = os.path.splitext(path)[1].lower()
    # Excel / ODS 走二进制解析。这里判两遍：扩展名对得上，或者内容确实是工作簿——
    # 用户常把 xlsx 改名成 .csv，只认扩展名会掉进「latin-1 解出一堆乱码」的死路。
    if extension in (".xlsx", ".xlsm", ".ods"):
        from .excel import read_excel

        return read_excel(path, sheet, notes)
    if raw.startswith(_ZIP_SIGNATURE):
        from .excel import is_excel_zip, read_excel

        if is_excel_zip(path):
            return read_excel(path, sheet, notes)
        raise ValueError(
            "这看起来是 Excel / ODS 等压缩容器格式，但里面没有工作簿结构："
            "如果是 .numbers，请用 Numbers / Excel 另存为 .xlsx 或 CSV"
        )
    if extension in _EXCEL_EXTENSIONS or raw.startswith(_OLE_SIGNATURE):
        raise ValueError(
            "不支持旧版 .xls 二进制格式（OLE 复合文档）：请用 Excel / WPS 打开后"
            "「另存为 .xlsx」或「另存为 CSV」，再导入生成的文件"
        )
    binary_hint = _detect_binary(raw)
    if binary_hint:
        raise ValueError(binary_hint)

    text = _decode(raw)
    if _looks_binary(text):
        raise ValueError("这看起来不是文本表格：文件里含大量不可打印字符（可能是二进制格式），请另存为 CSV 后再导入")

    if extension == ".json":
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            for key in ("rows", "data", "records", "items"):
                if isinstance(parsed.get(key), list):
                    parsed = parsed[key]
                    break
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("JSON 需要是对象数组，或包含 rows/data/records 字段的数组")
        columns, rows = _table_from_records(parsed)
        return columns, rows, os.path.basename(path)

    if extension in _JSONL_EXTENSIONS:
        records: List[object] = []
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "第 %d 行不是完整的 JSON（%s）。.jsonl / .ndjson 要求每行一个 JSON 对象" % (lineno, exc.msg)
                ) from exc
        if not records:
            raise ValueError("文件内容为空：.jsonl / .ndjson 里没有一行 JSON")
        columns, rows = _table_from_records(records)
        return columns, rows, os.path.basename(path)

    sample = text[:16384]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    # newline="" 交给 csv 模块自己处理换行：
    # 默认的 newline="\n" 会让「只有 CR 换行」的文件（旧版 Mac / 部分系统导出）
    # 直接抛出一句英文的 csv.Error，用户完全不知道该怎么办
    reader = csv.reader(io.StringIO(text, newline=""), dialect)
    try:
        records = [record for record in reader]
    except csv.Error as exc:
        raise ValueError("无法解析表格：%s" % exc) from exc
    if not records:
        raise ValueError("文件内容为空")
    header = _unique_headers(records[0])
    body = [record for record in records[1:] if any(str(cell).strip() for cell in record)]
    if not body:
        raise ValueError("文件只有表头，没有数据行")
    normalized = [
        [record[i] if i < len(record) else "" for i in range(len(header))]
        for record in body
    ]
    return header, normalized, os.path.basename(path)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        # 分隔符必须与前端 JSON.stringify 一致（紧凑无空格），
        # 否则同一份 JSON 在 CLI 报告和网页里会显示成两种样子
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)
