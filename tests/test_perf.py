"""性能与数值正确性回归：防止 O(n²) 统计实现回潮。

背景：std 曾把 sum(列) 写在生成器内部，导致每个元素都重新求和整列，
5 万行 × 6 列需要 42 秒。修复后为 0.7 秒量级，这里设一个宽松上限做护栏。
"""
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from glassdata.core import analyze_table, parse_date

failures = []


def check(name, condition, detail=""):
    if condition:
        print("  ok   " + name)
    else:
        failures.append(name)
        print("  FAIL " + name + (" :: " + str(detail) if detail else ""))


COLUMNS = ["id", "cat", "amount", "units", "when", "flag"]
random.seed(20261022)
ROWS = [
    [
        str(i),
        random.choice(["数码", "家居", "服饰", "美妆", "食品"]),
        "%.2f" % random.uniform(10, 9000),
        str(random.randint(1, 20)),
        "2026-%02d-%02d" % (random.randint(1, 12), random.randint(1, 28)),
        random.choice(["true", "false"]),
    ]
    for i in range(20000)
]

start = time.time()
payload = analyze_table(COLUMNS, ROWS, source="perf.csv")
elapsed = time.time() - start
print("  20,000 行 × 6 列耗时 %.2fs" % elapsed)
check("2 万行分析在 6 秒内完成", elapsed < 6.0, "%.2fs" % elapsed)

amount = [float(row[2]) for row in ROWS]
size = len(amount)
mean = sum(amount) / size
reference_std = math.sqrt(sum((value - mean) ** 2 for value in amount) / (size - 1))
profile = next(item for item in payload["columns"] if item["name"] == "amount")
check("标准差与逐元素参照实现一致", abs(profile["stats"]["std"] - reference_std) < 1e-4,
      "%s vs %s" % (profile["stats"]["std"], reference_std))
check("均值正确", abs(profile["stats"]["mean"] - mean) < 1e-4)

print("  日期解析吞吐")
dates = ["2026-%02d-%02d 10:20:30" % (random.randint(1, 12), random.randint(1, 28))
         for _ in range(20000)]
start = time.time()
for text in dates:
    parse_date(text)
elapsed = time.time() - start
print("  2 万次日期解析 %.2fs" % elapsed)
check("日期解析在 1 秒内完成", elapsed < 1.0, "%.2fs" % elapsed)

if failures:
    print("\nFAILED: " + ", ".join(failures))
    sys.exit(1)
print("\nALL_PERF_CHECKS_PASSED")
