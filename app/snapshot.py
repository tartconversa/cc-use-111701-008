"""最终内容的身份判定。

“最终内容身份”分两层：

1. **显式身份**：文档用 ``meta[name=campaign-id]``、``body[data-content-id]``
   或响应头 ``X-Content-Id`` 声明。相同声明即使来自不同二维码，也指向同一
   内容身份。
2. **实体身份**：没有显式声明时，用 SHA-256 表示实际收到的字节。它不是
   “页面标题”或 URL，所以同一内容经不同路径到达仍可比较。

无论哪种情况，每个二维码的链路证据都单独保存；共享身份不会合并路径。
"""
from __future__ import annotations

import hashlib
import re

_HEADERS = ("x-content-id", "x-campaign-id")
_ID_MAX = 200


def _header_value(headers, name):
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


def content_identity(body, headers, analysis=None):
    """返回 ``{'kind', 'value', 'source', 'sha256'}``。"""
    digest = hashlib.sha256(body).hexdigest()
    for name in _HEADERS:
        value = _header_value(headers, name)
        if value is None:
            continue
        value = value.strip()
        if not _valid(value):
            raise ValueError(f"响应头 {name} 的内容身份无效")
        return {"kind": "declared", "value": value, "source": f"header:{name}", "sha256": digest}
    if analysis is not None and analysis.explicit_content_id:
        value = analysis.explicit_content_id.strip()
        if not _valid(value):
            raise ValueError("文档声明的内容身份无效")
        return {
            "kind": "declared",
            "value": value,
            "source": analysis.content_id_source,
            "sha256": digest,
        }
    return {"kind": "digest", "value": digest, "source": "sha256", "sha256": digest}


def _valid(value):
    """内容身份只要求可打印、长度受限；不同站点可能用中文活动名。"""
    if not value or len(value) > _ID_MAX:
        return False
    if value != value.strip():
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return False
    return True


def same_identity(left, right):
    """两条内容身份是否指向同一最终页面。"""
    if not left or not right:
        return False
    return left.get("kind") == right.get("kind") and left.get("value") == right.get("value")
