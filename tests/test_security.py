"""安全与鲁棒性测试：XSS 逃逸、脚本截断、异常输入是否给得出中文提示。

数据工具最容易被忽略的一类问题是"用户的数据本身被当成代码执行"：
单元格里写 `<img onerror=...>`，如果渲染时没转义，打开报告就等于执行了别人的脚本。

跑法：python tests/test_security.py
"""
import json
import pathlib
import re
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from glassdata import core, render  # noqa: E402

TMP = pathlib.Path(tempfile.mkdtemp(prefix="sec_"))

PAYLOADS = [
    '<img src=x onerror="alert(1)">',
    "</script><script>alert(2)</script>",
    '"><svg onload=alert(3)>',
    '<iframe src="javascript:alert(4)">',
    "javascript:alert(5)",
    "{{7*7}}",
    '=cmd|"/c calc"!A1',
]

results = []


def check(name, ok, detail=""):
    results.append((ok, name, detail))


def check_payloads_in_html(html, tag):
    dangerous = [
        r"<img[^>]*onerror",
        r"<svg[^>]*onload",
        r"<iframe[^>]*javascript:",
        r"</script>\s*<script>alert",
    ]
    for pat in dangerous:
        hit = re.search(pat, html, re.I)
        check("%s 未执行载荷 %s" % (tag, pat), hit is None, hit.group(0)[:60] if hit else "")


def build_csv():
    lines = ["编号,备注,数值"]
    for i, payload in enumerate(PAYLOADS):
        lines.append('ID-%d,"%s",%d' % (i, payload.replace('"', '""'), i + 1))
    path = TMP / "xss.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main():
    try:
        return run()
    finally:
        # 测试自己产出的夹具要自己收走：每跑一次就在用户 Temp 里留一堆目录，
        # 跑一百次 CI 就是一百个。夹具是代码现场造的，删掉随时能重建。
        shutil.rmtree(str(TMP), ignore_errors=True)


def run():
    csv_path = build_csv()

    try:
        cols, rows, _ = core.read_table(str(csv_path))
        check("含恶意载荷的 CSV 可解析", len(rows) == len(PAYLOADS), "%d 行" % len(rows))
        check("载荷文本被原样保留（不静默丢弃）",
              any(PAYLOADS[0] in row[1] for row in rows))
    except Exception as exc:  # noqa: BLE001
        check("含恶意载荷的 CSV 可解析", False, repr(exc))
        return report()

    try:
        payload = core.analyze_table(cols, rows, source="xss.csv")
        html = render.build_html(payload, "注入测试")
        check("单文件报告可生成", len(html) > 2000, "%d 字节" % len(html))

        blocks = re.findall(r'<script[^>]*type="application/json"[^>]*>([\s\S]*?)</script>', html)
        check("报告含唯一数据块", len(blocks) == 1, str(len(blocks)))
        if blocks:
            try:
                parsed = json.loads(blocks[0])
                check("数据块是合法 JSON（脚本未被截断）", isinstance(parsed, dict))
            except Exception as exc:  # noqa: BLE001
                check("数据块是合法 JSON（脚本未被截断）", False, repr(exc)[:80])

        check_payloads_in_html(html, "单文件报告")
        check("脚本标签数量合理", len(re.findall(r"</script>", html)) <= 3,
              "%d 个 </script>" % len(re.findall(r"</script>", html)))
    except Exception as exc:  # noqa: BLE001
        check("单文件报告可生成", False, repr(exc))
        return report()

    # JSON 输入里的 </script> 同样不能截断数据块
    try:
        jpath = TMP / "xss.json"
        jpath.write_text(json.dumps([
            {"名称": "</script><script>alert(9)</script>", "值": 1},
            {"名称": "<img src=x onerror=alert(10)>", "值": 2},
        ], ensure_ascii=False), encoding="utf-8")
        cols2, rows2, _ = core.read_table(str(jpath))
        html2 = render.build_html(core.analyze_table(cols2, rows2, source="xss.json"), "JSON 注入测试")
        check_payloads_in_html(html2, "JSON 报告")
        blocks2 = re.findall(r'<script[^>]*type="application/json"[^>]*>([\s\S]*?)</script>', html2)
        ok2 = False
        if len(blocks2) == 1:
            try:
                json.loads(blocks2[0])
                ok2 = True
            except Exception:  # noqa: BLE001
                ok2 = False
        check("JSON 数据块未被截断", ok2)
    except Exception as exc:  # noqa: BLE001
        check("JSON 注入测试", False, repr(exc))

    # 异常输入要给出中文提示，而不是抛裸栈或英文内部报错
    cases = {
        "空文件": b"",
        "只有表头": "a,b,c\n".encode(),
        "只有空行": b"\n\n\n",
        "单列单值": "a\n1\n".encode("utf-8"),
        "Excel 文件": b"PK\x03\x04fake-zip-content",
    }
    for name, data in cases.items():
        path = TMP / ("case_%s.csv" % name)
        path.write_bytes(data)
        try:
            cols3, rows3, _ = core.read_table(str(path))
            core.analyze_table(cols3, rows3, source=name)
            expected_ok = name == "单列单值"
            check("异常输入处理：%s" % name, expected_ok, "该文件正常解析（单列单行本就合法）")
        except (ValueError, FileNotFoundError) as exc:
            message = str(exc)
            # 提示必须是中文，不能把 csv / json 模块的英文异常直接甩给用户
            has_cjk = bool(re.search(r"[\u4e00-\u9fff]", message))
            check("异常输入给中文提示：%s" % name, has_cjk, message[:50])
        except Exception as exc:  # noqa: BLE001
            check("异常输入给中文提示：%s" % name, False,
                  "裸异常 %s: %s" % (type(exc).__name__, exc))

    # 编码矩阵
    text = "名称,数量\n测试甲,1\n测试乙,2\n"
    encodings = {
        "utf-8-sig": text.encode("utf-8-sig"),
        "gbk": text.encode("gbk"),
        "utf-16": text.encode("utf-16"),
        "utf-16-be": b"\xfe\xff" + text.encode("utf-16-be"),
        "big5": "名稱,數量\n測試甲,1\n測試乙,2\n".encode("big5"),
    }
    for enc, data in encodings.items():
        path = TMP / ("enc_%s.csv" % enc)
        path.write_bytes(data)
        try:
            cols4, rows4, _ = core.read_table(str(path))
            check("编码可识别：%s" % enc, cols4[0] in ("名称", "名稱") and rows4[0][0].startswith(("测试", "測試")))
        except Exception as exc:  # noqa: BLE001
            check("编码可识别：%s" % enc, False, repr(exc)[:60])

    # 列数不一致的行不能越界
    ragged = TMP / "ragged.csv"
    ragged.write_text("a,b,c\n1,2\n1,2,3,4,5\n", encoding="utf-8")
    try:
        cols5, rows5, _ = core.read_table(str(ragged))
        widths = {len(r) for r in rows5}
        check("残缺行被补齐到表头宽度", widths == {len(cols5)}, str(widths))
        core.analyze_table(cols5, rows5, source="ragged.csv")
        check("残缺行不导致分析崩溃", True)
    except Exception as exc:  # noqa: BLE001
        check("残缺行被补齐到表头宽度", False, repr(exc)[:60])

    test_zip_bomb()
    test_dtd_rejected()
    test_column_bomb()
    test_csv_injection()
    test_js_guards()

    return report()


# --------------------------------------------------------------------------- #
# 解压炸弹：321 KB 的 .xlsx 可以解压成几百 GB
#
# 实测（2026-10）：一个 321 KB 的包，解压 145 MB，改造前 read_excel 峰值内存 581 MB。
# 放大十倍就是必然 OOM——而用户只会看到「工具卡死」。
# --------------------------------------------------------------------------- #
MINI_CT = (
    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
    "</Types>"
)
MINI_ROOT = (
    '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)
MINI_WB = (
    '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="S1" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
MINI_RELS = (
    '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
)
MINI_SHEET = (
    '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
    '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
    '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>1200</v></c></row>'
    "</sheetData></worksheet>"
)


def make_shared_bomb(path, repeat, payload="A" * 200):
    """造一个 sharedStrings 解压后约 repeat × 1 KB 的工作簿。"""
    import zipfile

    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("[Content_Types].xml", MINI_CT)
        archive.writestr("_rels/.rels", MINI_ROOT)
        archive.writestr("xl/workbook.xml", MINI_WB)
        archive.writestr("xl/_rels/workbook.xml.rels", MINI_RELS)
        archive.writestr("xl/worksheets/sheet1.xml", MINI_SHEET)
        with archive.open("xl/sharedStrings.xml", "w") as handle:
            handle.write(
                b'<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            )
            for _ in range(repeat):
                handle.write(("<si><t>%s</t></si>" % payload).encode("utf-8"))
            handle.write(b"</sst>")
    return path


def test_zip_bomb():
    from glassdata import excel as excel_mod
    from glassdata.excel import _GuardedPart, _size_text
    import io

    # 1) 守卫本身：超过预算就停，且文案说人话（小预算时是 KB，不是 0 MB）
    guard = _GuardedPart(io.BytesIO(b"x" * 4096), 1024, "xl/sharedStrings.xml")
    try:
        guard.read(4096)
        check("解压守卫在超预算时抛错", False, "没有抛错")
    except ValueError as exc:
        message = str(exc)
        check("解压守卫在超预算时抛错", True)
        check("预算提示用 KB 而不是 0 MB", "1 KB" in message and "0 MB" not in message, message[:60])
        check("预算提示点明是压缩炸弹", "压缩炸弹" in message, message[:60])
    check("预算文案 512 MB 正常呈现", _size_text(512 * 1024 * 1024) == "512 MB", _size_text(512 * 1024 * 1024))

    # 2) 真实工作簿：把预算压到 64 KB，一个 1 MB 的 sharedStrings 必须被拦下
    small = make_shared_bomb(TMP / "bomb-small.xlsx", 1024)
    original = excel_mod.PART_LIMIT
    try:
        excel_mod.PART_LIMIT = 64 * 1024
        try:
            excel_mod.read_excel(str(small))
            check("真实 xlsx 的压缩炸弹被拦下", False, "读成功了，说明预算没生效")
        except ValueError as exc:
            check("真实 xlsx 的压缩炸弹被拦下", "压缩炸弹" in str(exc), str(exc)[:70])
    finally:
        excel_mod.PART_LIMIT = original

    # 3) 同一份文件在正常预算下必须读得动（别把大文件误伤）
    try:
        columns, rows, _ = excel_mod.read_excel(str(small))
        check("正常预算下大 sharedStrings 可读", len(columns) == 2 and len(rows) == 1, str(columns))
    except Exception as exc:  # noqa: BLE001
        check("正常预算下大 sharedStrings 可读", False, repr(exc)[:70])


# --------------------------------------------------------------------------- #
# DTD / 实体展开：OOXML 里不可能出现 DOCTYPE，出现就一定是攻击
# 实测 400 字节能展开出 3 MB；不拦的话几十层就是内存耗尽。
# --------------------------------------------------------------------------- #
ENTITY_BOMB = (
    '<?xml version="1.0"?>\n'
    "<!DOCTYPE lolz [\n"
    ' <!ENTITY lol "lollollollollollollollollollol">\n'
    ' <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
    ' <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">\n'
    ' <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">\n'
    "]>\n"
    '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    "<si><t>&lol4;</t></si></sst>"
)


def test_dtd_rejected():
    import zipfile

    from glassdata.excel import read_excel

    path = TMP / "entity-bomb.xlsx"
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", MINI_CT)
        archive.writestr("_rels/.rels", MINI_ROOT)
        archive.writestr("xl/workbook.xml", MINI_WB)
        archive.writestr("xl/_rels/workbook.xml.rels", MINI_RELS)
        archive.writestr("xl/worksheets/sheet1.xml", MINI_SHEET)
        archive.writestr("xl/sharedStrings.xml", ENTITY_BOMB)

    try:
        read_excel(str(path))
        check("含 DOCTYPE 的工作簿被拒绝", False, "读成功了")
    except ValueError as exc:
        message = str(exc)
        check("含 DOCTYPE 的工作簿被拒绝", True)
        check("报错点明 DOCTYPE", "<!DOCTYPE" in message, message[:70])
        check("报错说明是防实体攻击", "billion laughs" in message or "XXE" in message, message[:80])

    # 合法 XML 不该被误伤：没有 DOCTYPE 的同一份结构必须读得通
    clean = TMP / "no-dtd.xlsx"
    with zipfile.ZipFile(str(clean), "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", MINI_CT)
        archive.writestr("_rels/.rels", MINI_ROOT)
        archive.writestr("xl/workbook.xml", MINI_WB)
        archive.writestr("xl/_rels/workbook.xml.rels", MINI_RELS)
        archive.writestr("xl/worksheets/sheet1.xml", MINI_SHEET)
        archive.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>城市</t></si><si><t>金额</t></si><si><t>北京</t></si></sst>",
        )
    try:
        columns, rows, _ = read_excel(str(clean))
        check("没有 DTD 的合法文件照常读", columns == ["城市", "金额"] and rows == [["北京", "1200"]],
              "%r %r" % (columns, rows))
    except Exception as exc:  # noqa: BLE001
        check("没有 DTD 的合法文件照常读", False, repr(exc)[:70])


# --------------------------------------------------------------------------- #
# 伪造坐标：列号是拿来算宽度的，`r="AAAAAAAA1"` 会算出 83 亿列
# `[cells.get(i, "") for i in range(width)]` 当场把内存吃光。
# Excel 的列上限是 XFD（16384），正常软件写不出更大的坐标。
# --------------------------------------------------------------------------- #
def test_column_bomb():
    import time
    import zipfile

    from glassdata.excel import read_excel

    def sheet_with(ref):
        return (
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1"><v>1</v></c><c r="%s"><v>2</v></c></row></sheetData></worksheet>'
            % ref
        )

    for ref in ("AAAAAAAA1", "ZZZZZZ1"):
        path = TMP / ("col-bomb-%s.xlsx" % ref[:4])
        with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", MINI_CT.replace(
                '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>',
                ""))
            archive.writestr("_rels/.rels", MINI_ROOT)
            archive.writestr("xl/workbook.xml", MINI_WB)
            archive.writestr("xl/_rels/workbook.xml.rels", MINI_RELS)
            archive.writestr("xl/worksheets/sheet1.xml", sheet_with(ref))
        started = time.time()
        try:
            read_excel(str(path))
            check("伪列号「%s」被拒绝" % ref, False, "读成功了")
        except ValueError as exc:
            elapsed = time.time() - started
            check("伪列号「%s」被拒绝" % ref, "列上限" in str(exc), str(exc)[:60])
            check("伪列号「%s」立刻失败而不是先吃内存" % ref, elapsed < 2.0, "%.2fs" % elapsed)

    # 上限之内的最宽列（XFD）必须照常读
    wide = TMP / "wide.xlsx"
    with zipfile.ZipFile(str(wide), "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", MINI_CT.replace(
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>',
            ""))
        archive.writestr("_rels/.rels", MINI_ROOT)
        archive.writestr("xl/workbook.xml", MINI_WB)
        archive.writestr("xl/_rels/workbook.xml.rels", MINI_RELS)
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1"><v>1</v></c><c r="XFD1"><v>2</v></c></row>'
            '<row r="2"><c r="A2"><v>3</v></c><c r="XFD2"><v>4</v></c></row></sheetData></worksheet>',
        )
    try:
        columns, rows, _ = read_excel(str(wide))
        check("XFD 这个合法上限仍然可读", len(columns) == 16384 and rows[0][-1] == "4",
              "%d 列，末格 %r" % (len(columns), rows[0][-1] if rows else None))
    except Exception as exc:  # noqa: BLE001
        check("XFD 这个合法上限仍然可读", False, repr(exc)[:70])


# --------------------------------------------------------------------------- #
# CSV 公式注入（CWE-1236）：导出的表被 Excel 双击打开时，
# 以 = + - @ 开头的格子会被当公式执行，不需要宏、不需要点「启用」。
# --------------------------------------------------------------------------- #
def test_csv_injection():
    from glassdata.clean import csv_neutralize, write_clean_csv

    hostile = [
        '=cmd|\'/C calc\'!A0',
        '=HYPERLINK("http://evil.example","点我")',
        "@SUM(1+1)*cmd|'/C calc'!A0",
        "\t1234",
    ]
    benign = ["-1200", "+5", "-1.5e3", "北京", "0x10"]
    for value in hostile:
        guarded = csv_neutralize(value)
        check("危险值被加前缀：%r" % value[:18], guarded == "'" + value, repr(guarded))
    for value in benign:
        check("安全值不改动：%r" % value, csv_neutralize(value) is None, repr(csv_neutralize(value)))
    check("负数是数字，不能被当公式", csv_neutralize("-1200") is None)
    check("单独一个减号也中和（它不是数字）", csv_neutralize("-") == "'-")
    check("带下划线的不是合法数字（与 JS 端一致）", csv_neutralize("-1_0") == "'-1_0")

    target = TMP / "clean-hostile.csv"
    stats = {}
    write_clean_csv(
        str(target),
        ["名称", "=标题", "-金额"],
        [['=cmd|\'/C calc\'!A0', "-1200", "@SUM(1+1)"], ["北京", "-1.5e3", "100"]],
        stats,
    )
    text = target.read_text(encoding="utf-8-sig")
    check("导出的 CSV 里危险单元格带单引号", "'=cmd|'/C calc'!A0" in text, text.splitlines()[1][:40])
    check("导出时表头也被中和", "'=标题" in text, text.splitlines()[0])
    check("负数保持原样", ",-1200," in text, text.splitlines()[1][:40])
    check("科学计数法负数保持原样", ",-1.5e3," in text, text.splitlines()[2][:40])
    check("统计到被中和的格数", stats.get("escaped") == 4, str(stats))
    check("导出文件仍是合法 CSV", len(list(__import__("csv").reader(text.splitlines()))) == 3, text[:40])


def test_js_guards():
    """前端对同一批恶意文件也必须拒绝——两端的防线不能只守一边。

    网页版是拖进去就能用的入口，用户不会先跑一遍命令行验证文件是否安全。
    """
    import json
    import shutil
    import subprocess
    import zipfile

    node = shutil.which("node")
    if not node:
        check("前端守卫（跳过：没有 node）", True)
        return

    # 现场准备三份恶意文件 + 一份对照
    bomb = make_shared_bomb(TMP / "js-bomb.xlsx", 512)
    col = TMP / "js-col.xlsx"
    with zipfile.ZipFile(str(col), "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", MINI_CT.replace(
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>',
            ""))
        archive.writestr("_rels/.rels", MINI_ROOT)
        archive.writestr("xl/workbook.xml", MINI_WB)
        archive.writestr("xl/_rels/workbook.xml.rels", MINI_RELS)
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1"><v>1</v></c><c r="AAAAAAAA1"><v>2</v></c></row></sheetData></worksheet>',
        )
    dtd = TMP / "js-dtd.xlsx"
    with zipfile.ZipFile(str(dtd), "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", MINI_CT)
        archive.writestr("_rels/.rels", MINI_ROOT)
        archive.writestr("xl/workbook.xml", MINI_WB)
        archive.writestr("xl/_rels/workbook.xml.rels", MINI_RELS)
        archive.writestr("xl/worksheets/sheet1.xml", MINI_SHEET)
        archive.writestr("xl/sharedStrings.xml", ENTITY_BOMB)
    good = TMP / "js-good.xlsx"
    with zipfile.ZipFile(str(good), "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", MINI_CT)
        archive.writestr("_rels/.rels", MINI_ROOT)
        archive.writestr("xl/workbook.xml", MINI_WB)
        archive.writestr("xl/_rels/workbook.xml.rels", MINI_RELS)
        archive.writestr("xl/worksheets/sheet1.xml", MINI_SHEET)
        archive.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>城市</t></si><si><t>金额</t></si><si><t>北京</t></si></sst>",
        )

    cases = [
        ("压缩炸弹", str(bomb), 64 * 1024, "压缩炸弹"),
        ("伪列号", str(col), None, "列上限"),
        ("DOCTYPE", str(dtd), None, "DOCTYPE"),
        ("合法文件", str(good), None, None),
    ]
    runner = TMP / "guards.js"
    runner.write_text(
        r"""
const fs = require("fs");
const GlassXlsx = require(process.argv[2]);
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
(async () => {
  const out = [];
  for (const item of cases) {
    try {
      const table = await GlassXlsx.read(
        fs.readFileSync(item.path),
        item.limit ? { maxPartBytes: item.limit } : undefined
      );
      out.push({ ok: true, columns: table.columns.length });
    } catch (error) {
      out.push({ ok: false, message: error.message });
    }
  }
  process.stdout.write(JSON.stringify(out));
})();
""",
        encoding="utf-8",
    )
    payload = TMP / "guards.json"
    payload.write_text(
        json.dumps(
            [{"path": path, "limit": limit} for _n, path, limit, _e in cases],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(runner), str(ROOT / "glassdata" / "xlsx.js"), str(payload)],
        capture_output=True, text=True, errors="replace",
    )
    if result.returncode != 0 or not result.stdout.strip():
        check("前端守卫可执行", False, result.stderr[:200])
        return
    outcomes = json.loads(result.stdout.strip().splitlines()[-1])
    for (name, _path, _limit, expect_keyword), outcome in zip(cases, outcomes):
        if expect_keyword is None:
            check("前端放行合法文件：" + name, outcome["ok"], outcome.get("message", "")[:70])
        elif outcome["ok"]:
            check("前端拦下 " + name, False, "读成功了")
        else:
            check("前端拦下 " + name, expect_keyword in outcome["message"], outcome["message"][:70])


def report():
    failed = [r for r in results if not r[0]]
    for ok, name, detail in results:
        print(("  ok   " if ok else "  FAIL ") + name + (("  " + detail) if detail else ""))
    print()
    print("共 %d 项，失败 %d 项" % (len(results), len(failed)))
    if failed:
        print("SECURITY_CHECKS_FAILED")
        return 1
    print("ALL_SECURITY_CHECKS_PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
