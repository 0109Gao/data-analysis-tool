"""内置合成示例数据（全部由随机数生成，不代表任何真实业务）。"""

from __future__ import annotations

import csv
import os
import random
from datetime import datetime, timedelta
from typing import List, Sequence, Tuple

REGIONS: Sequence[Tuple[str, float]] = (
    ("华东", 0.32),
    ("华南", 0.24),
    ("华北", 0.20),
    ("西南", 0.13),
    ("华中", 0.11),
)
CHANNELS: Sequence[Tuple[str, float]] = (
    ("小程序", 0.34),
    ("官网", 0.30),
    ("第三方平台", 0.24),
    ("线下门店", 0.12),
)
CATEGORY_WEIGHTS: Sequence[Tuple[str, float]] = (
    ("数码", 0.26),
    ("家居", 0.24),
    ("服饰", 0.20),
    ("美妆", 0.16),
    ("食品", 0.14),
)
CATEGORY_PRICES = {
    "数码": 520.0,
    "家居": 180.0,
    "服饰": 120.0,
    "美妆": 90.0,
    "食品": 45.0,
}
HEADER = [
    "订单号",
    "下单时间",
    "区域",
    "渠道",
    "品类",
    "件数",
    "单价",
    "折扣",
    "销售额",
    "评分",
    "是否退货",
]


def _weighted(rng: random.Random, table: Sequence[Tuple[str, float]]) -> str:
    cursor = rng.random()
    total = 0.0
    for label, weight in table:
        total += weight
        if cursor <= total:
            return label
    return table[-1][0]


def build_rows(count: int = 1400, seed: int = 20261022) -> List[List[str]]:
    """生成 count 行订单数据，列之间带有可验证的线性关系。"""
    rng = random.Random(seed)
    start = datetime(2025, 10, 1)
    rows: List[List[str]] = []
    for index in range(count):
        order_date = start + timedelta(
            days=rng.randint(0, 364), hours=rng.randint(8, 22), minutes=rng.randint(0, 59)
        )
        category = _weighted(rng, CATEGORY_WEIGHTS)
        base_price = CATEGORY_PRICES[category]
        unit_price = round(base_price * rng.uniform(0.78, 1.32), 2)
        units = rng.choice([1, 1, 1, 2, 2, 3, 3, 4, 5, 6, 8, 10, 12])
        discount = round(rng.choice([0, 0, 0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3]), 2)
        revenue = round(units * unit_price * (1 - discount), 2)
        returned = rng.random() < (0.14 - discount * 0.15)
        rating = 4.35 - (0.85 if returned else 0.0) - discount * 0.4 + rng.gauss(0, 0.42)
        rating = max(1.0, min(5.0, round(rating, 1)))
        rows.append(
            [
                f"ORD-{100000 + index}",
                order_date.strftime("%Y-%m-%d %H:%M"),
                _weighted(rng, REGIONS),
                _weighted(rng, CHANNELS),
                category,
                str(units),
                f"{unit_price:.2f}",
                f"{discount:.2f}",
                f"{revenue:.2f}",
                "" if rng.random() < 0.07 else f"{rating:.1f}",
                "是" if returned else "否",
            ]
        )
    return rows


def write_demo_csv(path: str, count: int = 1400) -> str:
    """把示例数据写成 CSV，返回绝对路径。"""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(build_rows(count))
    return os.path.abspath(path)
