"""登记表：门店、物料编号、印刷 URL 与允许的目标域名。

登记表是采集范围的唯一来源。二维码的印刷 URL 必须与登记一致；未登记的
``store_id``/``qr_id`` 不会被“猜”出来，而是作为输入错误报告。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .urls import URLPolicyError, authorize_url, canonical_host, normalize_url, split_http_url
from .viewport import Viewport, validate_viewport


@dataclass(frozen=True)
class Registration:
    qr_id: str
    store_id: str
    printed_url: str
    allowed_domains: tuple[str, ...]
    viewport: Viewport

    def as_dict(self):
        return {
            "qr_id": self.qr_id,
            "store_id": self.store_id,
            "printed_url": self.printed_url,
            "allowed_domains": list(self.allowed_domains),
            "viewport": self.viewport.as_dict(),
        }


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"登记字段 {field} 无效")
    if any(ch in value for ch in "\r\n\x00"):
        raise ValueError(f"登记字段 {field} 含控制字符")
    return value.strip()


def _domains(value, qr_id):
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{qr_id} 未登记允许的域名")
    domains = []
    for item in value:
        item = _text(item, "allowed_domains")
        probe = item[2:] if item.startswith("*.") else item
        if not probe or "/" in probe or ":" in probe or "@" in probe:
            raise ValueError(f"{qr_id} 的允许域名无效")
        try:
            canonical_host(probe)
        except URLPolicyError as exc:
            raise ValueError(f"{qr_id} 的允许域名无效") from exc
        if item not in domains:
            domains.append(item)
    return tuple(domains)


def _viewports(value, index):
    """取第 index 条的视口。

    ``viewports`` 为数组时可以按登记顺序一一对应；只给一个视口时对全部条目
    生效。登记条目内的 ``viewport`` 优先。
    """
    if isinstance(value, (list, tuple)):
        if not value:
            raise ValueError("视口配置为空")
        if len(value) == 1:
            return validate_viewport(value[0])
        if index >= len(value):
            raise ValueError("视口配置数量少于登记条目")
        return validate_viewport(value[index])
    return validate_viewport(value)


def load_registry(data):
    """从登记表对象生成 ``{store_id: {qr_id: Registration}}``。"""
    if isinstance(data, dict) and isinstance(data.get("registrations"), list):
        rows = data["registrations"]
        viewports = data.get("viewports", data.get("viewport"))
    elif isinstance(data, list):
        rows = data
        viewports = None
    else:
        raise ValueError("登记表必须包含 registrations 数组")
    if not rows:
        raise ValueError("登记表为空")
    table = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("登记条目必须是对象")
        qr_id = _text(row.get("qr_id"), "qr_id")
        store_id = _text(row.get("store_id"), "store_id")
        printed_url = _text(row.get("printed_url"), "printed_url")
        try:
            split_http_url(printed_url)
        except URLPolicyError as exc:
            raise ValueError(f"{qr_id} 的印刷 URL 无效") from exc
        domains = _domains(row.get("allowed_domains"), qr_id)
        viewport = _viewports(row.get("viewport", viewports), index)
        ok, _ = authorize_url(printed_url, domains)
        if not ok:
            raise ValueError(f"{qr_id} 的印刷 URL 不在允许的域名内")
        registration = Registration(qr_id, store_id, printed_url, domains, viewport)
        store = table.setdefault(store_id, {})
        if qr_id in store:
            raise ValueError(f"门店 {store_id} 的物料编号 {qr_id} 重复")
        store[qr_id] = registration
    return table


def load_registry_file(path):
    return load_registry(json.loads(Path(path).read_text(encoding="utf-8")))


class Registry:
    """按门店和物料编号领取任务，并校验二维码指向登记 URL。"""

    def __init__(self, table):
        self._table = table
        self._by_identity = {}
        for store_id, items in table.items():
            for qr_id, registration in items.items():
                self._by_identity[(qr_id, store_id)] = registration

    @classmethod
    def from_file(cls, path):
        return cls(load_registry_file(path))

    @classmethod
    def from_data(cls, data):
        return cls(load_registry(data))

    def claim(self, store_id, qr_id, *, scanned_url=None):
        if not isinstance(store_id, str) or not store_id.strip():
            raise ValueError("门店编号无效")
        if not isinstance(qr_id, str) or not qr_id.strip():
            raise ValueError("物料编号无效")
        registration = self._by_identity.get((qr_id.strip(), store_id.strip()))
        if registration is None:
            raise KeyError(f"未登记的门店/物料组合: {store_id}/{qr_id}")
        if scanned_url is not None:
            if not isinstance(scanned_url, str) or not scanned_url.strip():
                raise ValueError("扫码 URL 无效")
            if normalize_url(scanned_url) != normalize_url(registration.printed_url):
                raise ValueError(f"{registration.qr_id} 的扫码 URL 与登记不一致")
        return registration

    def stores(self):
        return sorted(self._table)

    def items(self, store_id):
        if store_id not in self._table:
            raise KeyError(f"未登记的门店: {store_id}")
        return [self._table[store_id][key].as_dict() for key in sorted(self._table[store_id])]
