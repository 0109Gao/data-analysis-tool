"""纯标准库读取 Excel：.xlsx / .xlsm（zip + XML）与 .ods（OpenDocument）。

为什么不用 openpyxl / pandas：这个项目的定位是「零第三方依赖，拷走目录就能跑」。
而 .xlsx 本质就是一个 zip，里面放着几份 XML——共享字符串表、工作表、样式表。
标准库的 zipfile + xml.etree 足够把「读表格」这件事办完：单元格类型、日期序列号、
多工作表都在这里处理掉，不需要引入任何外部包。

写回 Excel 不做（要维护样式、公式、合并区域，代价与收益不对等），
清洗后的结果导出为 CSV，Excel 双击就能打开。
"""

from __future__ import annotations

import math
import os
import re
import zipfile
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

Table = Tuple[List[str], List[List[str]], str]

# --------------------------------------------------------------------------- #
# 解压与解析的守卫
#
# .xlsx 本质是 zip，攻击者可以交给你一个 300 KB、解压后几百 GB 的包
# （2026 年实测：321 KB → 解压 145 MB → 解析峰值 581 MB，放大十倍就是必然 OOM）。
# zipfile 完全不拦，谁调用 read() 谁负责。
#
# 两道防线：
#   1. 字节预算——流式读取时逐块计数，超了立刻报错，内存不会跟着涨；
#   2. 拒绝 DTD——OOXML / ODS 里不可能出现 DOCTYPE。一旦出现，只可能是想借
#      实体展开放大体积（billion laughs，实测 400 字节能展开出 3 MB），
#      或者借外部实体读本机文件（XXE）。两种都不必兼容，直接拒。
#
# 扫描发生在数据交给 expat **之前**，所以 DTD 里的实体根本轮不到被解析。
# --------------------------------------------------------------------------- #
PART_LIMIT = 512 * 1024 * 1024   # 单个 XML 部件解压后上限（真实表格远小于此）
META_LIMIT = 32 * 1024 * 1024    # styles / workbook / rels 这几份永远很小
_CHUNK = 1 << 16
_DTD_HEAD = 12                   # 跨块匹配 DTD 关键字时要回退的字节数

# Excel 的硬上限：列 A..XFD（16384），行 1..1048576。
# 这两条不只是「规范」——列号是拿来算 width 的，一个伪造的 r="AAAAAAAA1"
# 会算出 83 亿列，`[cells.get(i, "") for i in range(width)]` 当场把内存吃光。
# 正常软件写不出这种坐标，出现即视为不可信输入。
MAX_COLUMNS = 16384
MAX_ROWS = 1048576


def _size_text(limit: int) -> str:
    """把字节数说成人话：512 MB / 100 KB。小于 1 MB 时别四舍五入成 0 MB。"""
    if limit >= 1024 * 1024:
        return "%d MB" % max(1, round(limit / 1024 / 1024))
    return "%d KB" % max(1, round(limit / 1024))


class _GuardedPart:
    """包一层解压流：限制总字节数，并拦下 DOCTYPE / ENTITY。

    只实现 read()——iterparse 与 _read_part 都只用它。
    """

    def __init__(self, handle, limit: int, what: str):
        self._handle = handle
        self._limit = limit
        self._what = what
        self._seen = 0
        self._tail = b""

    def read(self, size: int = -1) -> bytes:
        chunk = self._handle.read(size)
        if not chunk:
            return chunk
        self._seen += len(chunk)
        if self._seen > self._limit:
            raise ValueError(
                "「%s」解压后超过 %s，已停止读取。"
                "正常的表格不会有这么大，这更像是压缩炸弹。" % (self._what, _size_text(self._limit))
            )
        probe = self._tail + chunk
        for key in (b"<!DOCTYPE", b"<!ENTITY"):
            if key in probe:
                raise ValueError(
                    "「%s」里出现了 %s，这不是正常的表格文件。"
                    "为防实体展开攻击（billion laughs / XXE），已停止解析。"
                    % (self._what, key.decode())
                )
        self._tail = probe[-_DTD_HEAD:]
        return chunk

    def close(self) -> None:
        self._handle.close()


def _open_part(archive: zipfile.ZipFile, name: str, limit: int):
    """打开一个受守卫的解压流。缺少该部件时照旧抛 KeyError，由调用方决定怎么办。"""
    return _GuardedPart(archive.open(name), limit, name)


def _read_part(archive: zipfile.ZipFile, name: str, limit: int = META_LIMIT) -> bytes:
    """分块读完一个受守卫的部件，返回原始字节。"""
    handle = archive.open(name)
    with handle:
        guard = _GuardedPart(handle, limit, name)
        chunks: List[bytes] = []
        while True:
            chunk = guard.read(_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

# Excel 内置的日期 / 时间格式编号（ECMA-376 第 18.8.30 节）
_BUILTIN_DATE_IDS = set(range(14, 23)) | {27, 28, 29, 30, 31, 32, 33, 34, 35, 36}
_BUILTIN_DATETIME_IDS = {22} | set(range(45, 48)) | set(range(50, 59))
_BUILTIN_TIME_ONLY = {18, 19, 20, 21, 45, 46, 47}


def _local(tag: str) -> str:
    """去掉命名空间前缀：{uri}sheet → sheet。不同工具写出的命名空间并不统一。"""
    return tag.rsplit("}", 1)[-1]


def _col_index(ref: str) -> int:
    """A1 → 0，AB12 → 27。"""
    index = 0
    for char in ref:
        if char.isalpha():
            index = index * 26 + (ord(char.upper()) - 64)
        else:
            break
    return index - 1


def _format_number(value: float) -> str:
    """把浮点数还原成「人写字的样子」：整数不带 .0，小数不留浮点尾巴。"""
    if math.isnan(value) or math.isinf(value):
        return ""
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return text or "0"


def _format_serial(value: float, date1904: bool, kind: str) -> str:
    """Excel 的日期是一个「自基准日算起的天数」，小数部分是一天内的时刻。"""
    if value < 0:
        return _format_number(value)
    base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
    # Excel 沿用了 Lotus 1-2-3 的 bug：1900 年被当成闰年，序列号 60 是不存在的 2 月 29 日。
    # 所以 60 之前的序列号要换一个基准日才是对的。
    if not date1904 and value < 60:
        base = datetime(1899, 12, 31)
    days = int(math.floor(value))
    seconds = int(round((value - days) * 86400))
    moment = base + timedelta(days=days, seconds=seconds)
    if kind == "time":
        return moment.strftime("%H:%M:%S")
    if seconds % 86400 == 0:
        return moment.strftime("%Y-%m-%d")
    if seconds % 60 == 0:
        return moment.strftime("%Y-%m-%d %H:%M")
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _classify_format(fmt_id: int, code: Optional[str]) -> Optional[str]:
    """判断一个数字格式是日期、时间还是日期时间；都不是则返回 None。"""
    if fmt_id in _BUILTIN_TIME_ONLY:
        return "time"
    if fmt_id in _BUILTIN_DATETIME_IDS:
        return "datetime"
    if fmt_id in _BUILTIN_DATE_IDS:
        return "date"
    if not code:
        return None
    # 去掉 [Red] / [$-409] / [h] 这类开关和 "年" 这类引号字面量，剩下的才是格式骨架
    cleaned = re.sub(r"\[[^\]]*\]", "", code)
    cleaned = re.sub(r'"[^"]*"', "", cleaned)
    cleaned = re.sub(r"\\.", "", cleaned)
    lowered = cleaned.lower()
    has_date = ("y" in lowered) or ("d" in lowered)
    has_time = ("h" in lowered) or ("s" in lowered)
    if has_date and has_time:
        return "datetime"
    if has_date:
        return "date"
    if has_time:
        return "time"
    return None


def _collect_text(node, out: List[str]) -> None:
    """收集富文本里的所有 <t>，跳过 rPh（日文注音）。"""
    for child in node:
        tag = _local(child.tag)
        if tag == "rPh":
            continue
        if tag == "t":
            out.append(child.text or "")
        else:
            _collect_text(child, out)


def _style_kinds(archive: zipfile.ZipFile) -> Dict[int, str]:
    """cellXfs 下标 → 日期类型。单元格的 s 属性就是这个下标。"""
    kinds: Dict[int, str] = {}
    try:
        raw = _read_part(archive, "xl/styles.xml")
    except KeyError:
        return kinds
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return kinds

    custom: Dict[int, str] = {}
    for node in root.iter():
        if _local(node.tag) == "numFmt":
            try:
                custom[int(node.get("numFmtId") or 0)] = node.get("formatCode") or ""
            except ValueError:
                continue

    cell_xfs = None
    for node in root:
        if _local(node.tag) == "cellXfs":
            cell_xfs = node
            break
    if cell_xfs is None:
        return kinds
    for index, xf in enumerate(cell_xfs):
        try:
            fmt_id = int(xf.get("numFmtId") or 0)
        except ValueError:
            continue
        kind = _classify_format(fmt_id, custom.get(fmt_id))
        if kind:
            kinds[index] = kind
    return kinds


def _shared_strings(archive: zipfile.ZipFile) -> List[str]:
    """流式读出共享字符串表。

    这份文件是压缩炸弹最爱的落点：`<si><t>1000 个 A</t></si>` 重复十万次，
    压缩后 300 KB，整份读进内存再建树要 580 MB。改成 iterparse 后内存只跟
    单条字符串有关，与条目总数无关。
    """
    result: List[str] = []
    try:
        handle = archive.open("xl/sharedStrings.xml")
    except KeyError:
        return result
    with handle:
        guard = _GuardedPart(handle, PART_LIMIT, "xl/sharedStrings.xml")
        try:
            for _, elem in ET.iterparse(guard, events=("end",)):
                if _local(elem.tag) != "si":
                    continue
                parts: List[str] = []
                _collect_text(elem, parts)
                result.append("".join(parts))
                elem.clear()
        except ET.ParseError:
            return result
    return result


def _cell_text(cell, shared: Sequence[str], kinds: Dict[int, str], date1904: bool) -> str:
    kind = cell.get("t") or "n"
    raw_value = None
    inline = None
    for child in cell:
        tag = _local(child.tag)
        if tag == "v":
            raw_value = child.text
        elif tag == "is":
            inline = child

    if kind == "inlineStr":
        if inline is None:
            return ""
        parts: List[str] = []
        _collect_text(inline, parts)
        return "".join(parts)

    if raw_value is None:
        return ""
    if kind == "s":
        try:
            return shared[int(raw_value)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "TRUE" if str(raw_value).strip() in ("1", "true", "TRUE") else "FALSE"
    if kind in ("str", "e"):
        return raw_value
    try:
        number = float(raw_value)
    except ValueError:
        return raw_value

    style = cell.get("s")
    if style:
        try:
            fmt = kinds.get(int(style))
        except ValueError:
            fmt = None
        if fmt:
            return _format_serial(number, date1904, fmt)
    return _format_number(number)


def _workbook_sheets(archive: zipfile.ZipFile) -> List[Tuple[str, str]]:
    """返回 [(工作表名, zip 内路径)]，按工作簿里的顺序。"""
    try:
        workbook = ET.fromstring(_read_part(archive, "xl/workbook.xml"))
    except KeyError:
        raise ValueError("这看起来是压缩包，但不是 Excel 工作簿（缺少 xl/workbook.xml）")
    except ET.ParseError as exc:
        raise ValueError("Excel 工作簿结构损坏，无法解析：%s" % exc) from exc

    rels: Dict[str, str] = {}
    try:
        rel_root = ET.fromstring(_read_part(archive, "xl/_rels/workbook.xml.rels"))
        for node in rel_root:
            if _local(node.tag) != "Relationship":
                continue
            target = node.get("Target") or ""
            if target.startswith("/"):
                target = target.lstrip("/")
            elif not target.startswith("xl/"):
                target = "xl/" + target
            rels[node.get("Id") or ""] = target
    except (KeyError, ET.ParseError):
        pass

    sheets: List[Tuple[str, str]] = []
    fallback = 1
    for node in workbook.iter():
        if _local(node.tag) != "sheet":
            continue
        name = node.get("name") or f"Sheet{fallback}"
        rel_id = ""
        for key, value in node.attrib.items():
            if _local(key) == "id":
                rel_id = value
                break
        target = rels.get(rel_id)
        if not target:
            target = f"xl/worksheets/sheet{fallback}.xml"
        sheets.append((name, target))
        fallback += 1
    if not sheets:
        raise ValueError("工作簿里没有任何工作表")
    return sheets


def _date1904(archive: zipfile.ZipFile) -> bool:
    try:
        workbook = ET.fromstring(_read_part(archive, "xl/workbook.xml"))
    except (KeyError, ET.ParseError):
        return False
    for node in workbook.iter():
        if _local(node.tag) == "workbookPr":
            return str(node.get("date1904") or "").lower() in ("1", "true")
    return False


_REF_PATTERN = re.compile(r"^([A-Za-z]+)(\d+)$")


def _ref_cell(ref: str):
    """A1 → (行 0, 列 0)。列名与行号都必须是合法写法，否则返回 None。"""
    match = _REF_PATTERN.match((ref or "").strip())
    if not match:
        return None
    return int(match.group(2)) - 1, _col_index(match.group(1))


def _merge_range(ref: str):
    """A1:C2 → (r1, c1, r2, c2)，都是 0 起。写法不合规就忽略。"""
    if not ref or ":" not in ref:
        return None
    left, _, right = ref.partition(":")
    start, end = _ref_cell(left), _ref_cell(right)
    if not start or not end:
        return None
    return (
        min(start[0], end[0]),
        min(start[1], end[1]),
        max(start[0], end[0]),
        max(start[1], end[1]),
    )


def _rows_from_sheet(archive: zipfile.ZipFile, path: str, shared, kinds, date1904: bool):
    """流式读取一张工作表，返回 (行, 合并区域, 每行的物理行号)。

    合并信息出现在 sheetData 之后（mergeCells 元素），所以能一边攒行一边攒范围。
    物理行号必须一起带出去：`<row r="5"/>` 这种空行不会被收进来，
    一旦被丢掉，「第 0 行有合并」的判断就会错位，标题行也就认不出来了。
    """
    rows: List[List[str]] = []
    physical: List[int] = []
    merge_refs: List[Tuple[int, int, int, int]] = []
    try:
        stream = archive.open(path)
    except KeyError:
        raise ValueError("工作簿里找不到工作表文件：%s" % path)
    running = 0
    # 流式解析：只保留「当前一行」，几十万行也不会把内存吃满
    with stream:
        for _, elem in ET.iterparse(
            _GuardedPart(stream, PART_LIMIT, path), events=("end",)
        ):
            tag = _local(elem.tag)
            if tag == "mergeCell":
                span = _merge_range(elem.get("ref") or "")
                if span:
                    merge_refs.append(span)
                elem.clear()
                continue
            if tag != "row":
                continue
            try:
                row_number = int(elem.get("r") or "")
            except ValueError:
                row_number = running + 1
            running = row_number
            cells: Dict[int, str] = {}
            position = 0
            for cell in elem:
                if _local(cell.tag) != "c":
                    continue
                ref = cell.get("r") or ""
                index = _col_index(ref) if ref else position
                if index >= MAX_COLUMNS:
                    raise ValueError(
                        "单元格坐标「%s」超出了 Excel 的列上限（A..XFD），"
                        "这份文件的结构不可信，已停止读取。" % ref
                    )
                cells[index] = _cell_text(cell, shared, kinds, date1904)
                position = index + 1
            if cells:
                width = max(cells) + 1
                rows.append([cells.get(index, "") for index in range(width)])
                physical.append(row_number - 1)
            elem.clear()
    return rows, merge_refs, physical


# 「自动挑工作表」的三条提示。前端 xlsx.js 里是同名常量，文案必须逐字一致，
# 否则差分测试会失败——那正是提示「两端走了不同分支」的信号。
NARROW_NOTE = "工作表「%s」只有一列、行数很少，更像说明文字，已自动改用「%s」"
EMPTY_NOTE = "工作表「%s」没有可分析的数据，已自动改用「%s」"
LAST_RESORT_NOTE = "工作簿里没有更像数据表的工作表，已使用只有一列的「%s」"

# 单列表「行数很少」的界限。封面/说明页几乎都在这个范围内，
# 而真正的单列清单通常远超 20 行，不会被误跳过。
NARROW_ROWS = 20


def _auto_pick(candidates, path: str, notes: Optional[List[str]]) -> Table:
    """不指定工作表时，从多张表里挑第一张「像数据表」的。

    candidates: [(表名, 取行函数)]，取行函数返回 (rows, merges, physical)。

    两个坑：
      1. 「封面页 + 数据页」的工作簿，第一张表只有一句说明，照第一张读会直接报错，
         用户以为文件坏了；
      2. 封面页恰好有两行以上文字时，能通过「表头 + 一行数据」的校验，
         于是被当成只有一列的表返回，真正的数据表反而没读到、也不报错。

    第 2 种是静默的错误，危害比第 1 种大。单列数据表在现实中极罕见，
    而封面/说明页几乎都是一列短文，所以「只有一列且行数很少」先跳过、留作兜底。
    """
    total = len(candidates)
    tried: List[str] = []
    narrow = None
    narrow_name = ""
    for index, (name, loader) in enumerate(candidates):
        rows, merges, physical = loader()
        collected: List[str] = []
        try:
            table = _assemble(rows, path, name, total, merges, collected, physical)
        except ValueError as exc:
            tried.append(str(exc))
            continue
        if total > 1 and len(table[0]) < 2 and len(table[1]) <= NARROW_ROWS:
            if narrow is None:
                narrow = (table, list(collected))
                narrow_name = name
            continue
        if notes is not None:
            notes.extend(collected)
            if narrow is not None:
                notes.append(NARROW_NOTE % (narrow_name, name))
            elif index:
                notes.append(EMPTY_NOTE % (candidates[0][0], name))
        return table
    if narrow is not None:
        table, collected = narrow
        if notes is not None:
            notes.extend(collected)
            notes.append(LAST_RESORT_NOTE % narrow_name)
        return table
    raise ValueError(
        "工作簿里的 %d 个工作表都没有可分析的数据（表头 + 至少一行数据）：%s"
        % (total, "；".join(tried))
    )


def _read_xlsx(path: str, sheet: Optional[str], notes: Optional[List[str]] = None) -> Table:
    with zipfile.ZipFile(path) as archive:
        sheets = _workbook_sheets(archive)
        shared = _shared_strings(archive)
        kinds = _style_kinds(archive)
        date1904 = _date1904(archive)
        if sheet:
            name, target = _pick_sheet(sheets, sheet, path)
            rows, merges, physical = _rows_from_sheet(archive, target, shared, kinds, date1904)
            return _assemble(rows, path, name, len(sheets), merges, notes, physical)
        return _auto_pick(
            [
                (name, (lambda target=target: _rows_from_sheet(
                    archive, target, shared, kinds, date1904)))
                for name, target in sheets
            ],
            path,
            notes,
        )


# --------------------------------------------------------------------------- #
# ODS（LibreOffice / WPS 另存为 .ods）
# --------------------------------------------------------------------------- #
_ODS_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_ODS_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"


def _ods_value(cell, text_parts: List[str]) -> str:
    value_type = cell.get("{%s}value-type" % _ODS_OFFICE) or ""
    if value_type in ("float", "percentage", "currency"):
        raw = cell.get("{%s}value" % _ODS_OFFICE)
        if raw is None:
            return "".join(text_parts)
        try:
            return _format_number(float(raw))
        except ValueError:
            return raw
    if value_type == "date":
        return (cell.get("{%s}date-value" % _ODS_OFFICE) or "").replace("T", " ").rstrip("Z")
    if value_type == "time":
        return (cell.get("{%s}time-value" % _ODS_OFFICE) or "").lstrip("PT").replace("H", ":").replace("M", ":").rstrip(":")
    if value_type == "boolean":
        return "TRUE" if (cell.get("{%s}boolean-value" % _ODS_OFFICE) or "").lower() == "true" else "FALSE"
    return "".join(text_parts)


def _ods_table_rows(table) -> Tuple[List[List[str]], List[Tuple[int, int, int, int]], List[int]]:
    """把一张 ODS 表拆成「行 + 合并区域 + 物理行号」，与 xlsx 侧保持同一套语义。

    ODS 的合并不是 mergeCells 列表，而是单元格上的 number-columns-spanned：
    主单元格后面跟着同样数量的 covered-table-cell 占位。
    """
    rows: List[List[str]] = []
    merges: List[Tuple[int, int, int, int]] = []
    physical: List[int] = []
    for row_index, row_node in enumerate(table):
        if _local(row_node.tag) != "table-row":
            continue
        cells: List[str] = []
        for cell in row_node:
            tag = _local(cell.tag)
            if tag not in ("table-cell", "covered-table-cell"):
                continue
            parts: List[str] = []
            for para in cell.iter():
                if _local(para.tag) == "p":
                    parts.append("".join(node.text or "" for node in para.iter() if _local(node.tag) == "s" or node.text))
            text = _ods_value(cell, parts).strip()
            span = cell.get("{%s}number-columns-spanned" % _ODS_TABLE)
            try:
                spanned = min(int(span), 1024) if span else 1
            except ValueError:
                spanned = 1
            if spanned > 1 and text:
                merges.append((row_index, len(cells), row_index, len(cells) + spanned - 1))
            repeat = cell.get("{%s}number-columns-repeated" % _ODS_TABLE)
            try:
                times = min(int(repeat), 1024) if repeat else 1
            except ValueError:
                times = 1
            cells.extend([text] * times)
        while cells and cells[-1] == "":
            cells.pop()
        if cells:
            rows.append(cells)
            physical.append(row_index)
    return rows, merges, physical


def _read_ods(path: str, sheet: Optional[str], notes: Optional[List[str]] = None) -> Table:
    with zipfile.ZipFile(path) as archive:
        try:
            raw = _read_part(archive, "content.xml", PART_LIMIT)
        except KeyError:
            raise ValueError("这看起来是压缩包，但不是 ODS 表格（缺少 content.xml）")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("ODS 结构损坏，无法解析：%s" % exc) from exc

    tables = [node for node in root.iter() if _local(node.tag) == "table" and node.get("{%s}name" % _ODS_TABLE)]
    if not tables:
        raise ValueError("ODS 里没有任何工作表")
    names = [node.get("{%s}name" % _ODS_TABLE) for node in tables]
    if sheet:
        index = _pick_index(names, sheet, path)
        rows, merges, physical = _ods_table_rows(tables[index])
        return _assemble(rows, path, names[index], len(names), merges, notes, physical)
    return _auto_pick(
        [
            (name, (lambda table=table: _ods_table_rows(table)))
            for name, table in zip(names, tables)
        ],
        path,
        notes,
    )


# --------------------------------------------------------------------------- #
# 公共入口
# --------------------------------------------------------------------------- #
def _pick_index(names: Sequence[str], sheet: Optional[str], path: str) -> int:
    if sheet is None or sheet == "":
        return 0
    text = str(sheet).strip()
    if text.isdigit():
        index = int(text)
        # 用户说的「第 1 个工作表」就是 index 1
        if 1 <= index <= len(names):
            return index - 1
        raise ValueError("工作表序号超出范围：共 %d 个，收到 %s" % (len(names), text))
    if text in names:
        return list(names).index(text)
    raise ValueError("找不到工作表「%s」，可选：%s" % (text, "、".join(names)))


def _pick_sheet(sheets: Sequence[Tuple[str, str]], sheet: Optional[str], path: str) -> Tuple[str, str]:
    index = _pick_index([name for name, _ in sheets], sheet, path)
    return sheets[index]


def _looks_like_header(cells: Sequence[str]) -> bool:
    """判断一行「像表头」：非空单元格里至少六成不是数字。

    只看是不是数字，不看长度：`金额(元)` 是表头，`1,234` 是数据，
    这两者用 parse_number 一试就分开了，不需要猜词性。
    """
    from .core import parse_number  # 就地导入，避免循环引用

    filled = [cell for cell in cells if str(cell).strip()]
    if not filled:
        return False
    text = sum(1 for cell in filled if parse_number(str(cell)) is None)
    return text * 10 >= len(filled) * 6


def _fill_row_merges(row: Sequence[str], spans) -> List[str]:
    """把横向合并的父表头摊到它覆盖的每一列上：`销售|` → `销售|销售`。"""
    filled = list(row)
    for _r1, c1, _r2, c2 in spans:
        if c1 >= len(filled):
            continue
        value = filled[c1]
        for column in range(c1, min(c2 + 1, len(filled))):
            filled[column] = value
    return filled


def _header_rows(rows: List[List[str]], merges, notes: List[str]):
    """决定哪一行当表头，返回 (表头单元格, 数据从第几行开始)。

    真实表格有两个坑，都会让「第一行就是表头」这个假设失效：

    1. **标题行**——第一行是合并居中的「销售明细表」，第二行才是真表头。
       照第一条行当表头，表会缩成一列，金额与日期全丢，而且不报错。
    2. **两级表头**——「销售」横跨两列，第二行是「金额 / 数量」。
       照第一条行当表头，列名会变成 销售 / col_2 / 客户。

    判据只看结构，不靠猜语种：整行只有一个不同取值且横向合并（或本来就只有一个格子）
    → 标题行；第一行有横向合并且第二行像表头 → 两级表头，拼成「父-子」。
    """
    head = rows[0]
    if len(rows) < 2:
        return head, 1
    tail = rows[1]
    spans = [span for span in merges if span[0] == 0 and span[3] > span[1]]
    if not spans and len(head) > 1:
        return head, 1
    values = {str(cell).strip() for cell in head if str(cell).strip()}
    if not _looks_like_header(tail) or len([c for c in tail if str(c).strip()]) < 2:
        return head, 1

    if len(values) <= 1:
        title = next(iter(values), "")
        notes.append(
            "已跳过开头的「%s」标题行（合并单元格），改用下一行作表头" % title
        )
        return tail, 2
    if spans:
        parents = _fill_row_merges(head, spans)
        combined = []
        for index, child in enumerate(tail):
            parent = str(parents[index]).strip() if index < len(parents) else ""
            kid = str(child).strip()
            if parent and kid and parent != kid:
                combined.append("%s-%s" % (parent, kid))
            else:
                combined.append(kid or parent)
        notes.append("检测到两级表头，已合并为「父-子」形式（如 销售-金额）")
        return combined, 2
    return head, 1


def _assemble(
    rows: List[List[str]],
    path: str,
    sheet_name: str,
    total_sheets: int,
    merges=None,
    notes: Optional[List[str]] = None,
    physical: Optional[List[int]] = None,
) -> Table:
    # 去掉整行为空的行（Excel 常在末尾留下大量空行，是格式残留不是数据）
    flags = [any(str(cell).strip() for cell in row) for row in rows]
    if physical is None or len(physical) != len(rows):
        physical = list(range(len(rows)))
    rows = [row for row, keep in zip(rows, flags) if keep]
    kept_physical = [number for number, keep in zip(physical, flags) if keep]
    if not rows:
        raise ValueError("工作表「%s」是空的：没有表头也没有数据" % sheet_name)
    # 合并区域记的是物理行号；空行被删掉以后行号会前移。
    # 表头判断只看前几行，所以只给前几行建一张「物理行号 → 下标」的表就够。
    lookup = {number: index for index, number in enumerate(kept_physical[:8])}
    shifted = []
    for r1, c1, r2, c2 in merges or []:
        start = lookup.get(r1)
        if start is None:
            continue
        shifted.append((start, c1, start, c2))
    if notes is None:
        notes = []
    header, start = _header_rows(rows, shifted, notes)
    header = [str(cell).strip() for cell in header]
    while header and header[-1] == "":
        header.pop()
    if not header:
        raise ValueError("工作表「%s」的第一行是空的，无法作为表头" % sheet_name)
    # 表头去重、补齐
    from .core import _unique_headers  # 就地导入，避免循环引用

    header = _unique_headers(header)
    body = []
    for row in rows[start:]:
        # 单元格内容不 strip：脏数据要留给清洗环节去处理，
        # 读取层偷偷改数据，用户就永远看不到「原始文件里到底有没有空格」。
        body.append([str(row[i]) if i < len(row) else "" for i in range(len(header))])
    if not body:
        raise ValueError("工作表「%s」只有表头，没有数据行" % sheet_name)
    display = os.path.basename(path)
    if total_sheets > 1:
        display = "%s · %s" % (display, sheet_name)
    return header, body, display


def list_sheets(path: str) -> List[str]:
    """列出工作簿里的所有工作表名，供命令行提示用。"""
    extension = os.path.splitext(path)[1].lower()
    try:
        if extension == ".ods":
            with zipfile.ZipFile(path) as archive:
                root = ET.fromstring(_read_part(archive, "content.xml", PART_LIMIT))
            return [
                node.get("{%s}name" % _ODS_TABLE) or ""
                for node in root.iter()
                if _local(node.tag) == "table" and node.get("{%s}name" % _ODS_TABLE)
            ]
        with zipfile.ZipFile(path) as archive:
            return [name for name, _ in _workbook_sheets(archive)]
    except (zipfile.BadZipFile, KeyError, ValueError):
        return []


def read_excel(
    path: str, sheet: Optional[str] = None, notes: Optional[List[str]] = None
) -> Table:
    """读取 Excel 工作簿，返回 (列名, 行数据, 显示名)。sheet 可为名称或 1 起的序号。

    notes 可选：传入一个列表，读取过程中的「跳过了标题行」「自动换了一张表」这类
    提示会追加进去，由调用方决定怎么展示。不传就等于不要这些提示。
    """
    extension = os.path.splitext(path)[1].lower()
    if extension == ".ods":
        return _read_ods(path, sheet, notes)
    with zipfile.ZipFile(path) as archive:
        # .xlsx / .xlsm 都是 zip；用内容判断而不是扩展名，改名成 .csv 也拦得住
        names = archive.namelist()
        if "xl/workbook.xml" not in names:
            raise ValueError(
                "这个压缩包里没有 Excel 工作簿结构（xl/workbook.xml），"
                "可能是 .numbers 或别的压缩格式：请用 Excel / WPS 另存为 .xlsx 或 CSV"
            )
    return _read_xlsx(path, sheet, notes)


def is_excel_zip(path: str) -> bool:
    """判断一个 zip 是不是 Excel / ODS（扩展名不可信时用）。"""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        return "xl/workbook.xml" in names or "content.xml" in names
    except (zipfile.BadZipFile, OSError):
        return False
