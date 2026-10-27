#!/usr/bin/env python3
"""一键跑完全部测试，并汇总每套的项数。

用法：
    python tests/run_all.py

设计说明：
- 只用标准库，不引入 pytest 之类的测试框架，与项目「零依赖」的定位一致；
- 每个测试文件自己就是可执行脚本，输出以 `  ok ...` 为一行一断言，
  末尾打印 `ALL_..._PASSED` 或 `FAILED`，本脚本据此统计；
- Node 找不到时会把 JS 那几套标为「跳过」而不是直接报错，
  这样只想验证 Python 端的人也能拿到部分结果。
"""

import glob
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
OK_LINE = re.compile(r"^\s+ok\b", re.M)


def find_node():
    """按「环境变量 → PATH → 常见安装位置」的顺序找 node。"""
    override = os.environ.get("NODE_BIN")
    if override and os.path.exists(override):
        return override
    for name in ("node", "node.exe"):
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
    ]
    candidates += glob.glob(
        os.path.expanduser("~/.workbuddy/binaries/node/versions/*/node.exe")
    )
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def run_one(interpreter, script):
    try:
        proc = subprocess.run(
            [interpreter, script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return 0, True, "超时（600 秒）", ""
    output = (proc.stdout or "") + (proc.stderr or "")
    passed = len(OK_LINE.findall(output))
    detail = ""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("ALL_") or stripped.startswith("FAILED"):
            detail = stripped
    failed = proc.returncode != 0 or "FAILED" in output
    return passed, failed, detail, output


def main():
    node = find_node()
    jobs = []
    for path in sorted(glob.glob(os.path.join(TESTS, "test_*.py"))):
        jobs.append(("py", path))
    for path in sorted(glob.glob(os.path.join(TESTS, "test_*.js"))):
        jobs.append(("js", path))

    if not jobs:
        print("没有找到测试文件")
        return 1

    name_width = max(len(os.path.basename(p)) for _, p in jobs) + 2
    total = 0
    failures = []
    skipped = []

    for kind, path in jobs:
        name = os.path.basename(path)
        if kind == "js" and not node:
            skipped.append(name)
            print("%-*s %5s  %s" % (name_width, name, "-", "跳过（找不到 node）"))
            continue
        interpreter = sys.executable if kind == "py" else node
        count, failed, detail, output = run_one(interpreter, path)
        total += count
        status = detail or ("FAILED" if failed else "PASSED")
        print("%-*s %5d  %s" % (name_width, name, count, status))
        if failed:
            failures.append(name)
            tail = "\n".join(output.strip().splitlines()[-25:])
            print("---- %s 失败输出 ----\n%s\n---------------------" % (name, tail))

    print("")
    print("=" * 62)
    print("合计 %d 项，%d 套通过，%d 套失败，%d 套跳过"
          % (total, len(jobs) - len(failures) - len(skipped), len(failures), len(skipped)))
    if skipped:
        print("跳过的套件：%s（装上 node 后重跑即可）" % "、".join(skipped))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
