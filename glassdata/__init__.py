"""glassdata —— 液态玻璃风格的数据处理与可视化小工具。

对外暴露的核心接口：
    read_table(path)     读取 CSV / TSV / JSON 表格
    analyze_table(...)   对表格做类型推断与统计画像，返回可序列化的 payload
    build_html(payload)  把 payload 渲染成自包含的深色极光 HTML 报告
"""

from .core import analyze_table, read_table
from .render import build_html, write_report

__all__ = ["read_table", "analyze_table", "build_html", "write_report"]
__version__ = "1.0.0"
