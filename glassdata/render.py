"""渲染层：把 payload 注入自包含的 HTML 模板。"""

from __future__ import annotations

import html
import json
import os
from typing import Any, Dict, Optional

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")


def build_html(
    payload: Dict[str, Any],
    title: Optional[str] = None,
    head_extra: str = "",
) -> str:
    """生成完整的 HTML 字符串（CSS / JS / 数据全部内联，可离线打开）。

    head_extra 用于站点版注入 PWA 相关标签；单文件报告传空字符串即可，
    这样报告本身不会引用任何外部资源。
    """
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as handle:
        template = handle.read()

    meta = payload.setdefault("meta", {})
    if title:
        meta["title"] = title
    page_title = meta.get("title") or "数据分析"

    payload_json = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str)
    # 把所有 "<" 转成 JSON 合法的 \u003c 转义：
    # 既能避免数据里的 </script> 提前闭合脚本块，也不改变解析后的取值。
    payload_json = payload_json.replace("<", "\\u003c")

    return (
        template.replace("__TITLE__", html.escape(page_title))
        .replace("__PAYLOAD__", payload_json)
        .replace("__HEAD_EXTRA__", head_extra)
    )


def write_report(
    payload: Dict[str, Any], output_path: str, title: Optional[str] = None
) -> str:
    """把报告写到磁盘，返回绝对路径。"""
    markup = build_html(payload, title=title)
    directory = os.path.dirname(os.path.abspath(output_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(markup)
    return os.path.abspath(output_path)
