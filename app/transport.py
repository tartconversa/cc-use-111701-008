"""来源响应传输：不自动跟随跳转，由落地链路引擎逐跳决定。

``HttpTransport`` 只负责一次请求/一次响应。3xx 的 Location 由调用方读取，
因此重定向链、授权检查和循环判断都留在可审计的链路层。``MappingTransport``
用固定映射响应同一接口，便于在离线演练和测试中复现完整站点。
"""
from __future__ import annotations

import base64
import binascii
import json
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .urls import normalize_url, split_http_url


class TransportError(Exception):
    """一次请求没有拿到可用的 HTTP 响应。"""

    def __init__(self, kind, detail):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class TransportResponse:
    request_url: str
    response_url: str
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name):
        return next((v for k, v in self.headers if k.lower() == name.lower()), None)

    @property
    def content_type(self):
        return self.header("content-type")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class HttpTransport:
    """使用标准库发出的单次 GET，不读取环境代理，不跟随 3xx。"""

    def __init__(self, *, timeout=5.0):
        self.timeout = timeout

    def fetch(self, url, *, headers=None, max_bytes=1048576):
        split_http_url(url)
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("读取预算无效")
        request = urllib.request.Request(url, headers=dict(headers or {}))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            stream = opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            stream = exc
        except (socket.timeout, TimeoutError) as exc:
            raise TransportError("timeout", "请求超时") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise TransportError("timeout", "请求超时") from exc
            if isinstance(reason, ssl.SSLError):
                raise TransportError("tls", "TLS 握手失败") from exc
            if isinstance(reason, socket.gaierror):
                raise TransportError("dns", "域名解析失败") from exc
            raise TransportError("connection", "连接失败") from exc
        try:
            with stream:
                body = stream.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise TransportError("too_large", "响应超过读取预算")
                return TransportResponse(
                    request_url=url,
                    response_url=stream.geturl(),
                    status=int(stream.code),
                    headers=tuple(stream.headers.raw_items()),
                    body=body,
                )
        except TransportError:
            raise
        except (socket.timeout, TimeoutError) as exc:
            raise TransportError("timeout", "读取响应超时") from exc
        except OSError as exc:
            raise TransportError("connection", "读取响应失败") from exc


def _headers_from(value):
    if value is None:
        return ()
    if isinstance(value, dict):
        pairs = [(str(k), str(v)) for k, v in value.items()]
    elif isinstance(value, (list, tuple)):
        pairs = []
        for item in value:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError("映射响应头无效")
            pairs.append((str(item[0]), str(item[1])))
    else:
        raise ValueError("映射响应头无效")
    return tuple(pairs)


def _response_from_spec(entry, request_url, max_bytes):
    if not isinstance(entry, dict):
        raise ValueError("映射响应必须是对象")
    if "error" in entry:
        error = entry["error"]
        if isinstance(error, dict):
            raise TransportError(str(error.get("kind", "unmapped")), str(error.get("detail", "预置传输错误")))
        raise TransportError("unmapped", str(error))
    status = entry.get("status", 200)
    if type(status) is not int or not 100 <= status <= 599:
        raise ValueError("映射响应状态无效")
    headers = _headers_from(entry.get("headers"))
    if "body_base64" in entry:
        try:
            body = base64.b64decode(entry["body_base64"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("映射响应实体编码无效") from exc
    elif "body" in entry:
        raw = entry["body"]
        if not isinstance(raw, str):
            raise ValueError("映射响应实体必须是文本")
        body = raw.encode("utf-8")
    else:
        body = b""
    if len(body) > max_bytes:
        raise TransportError("too_large", "响应超过读取预算")
    response_url = entry.get("response_url", request_url)
    split_http_url(response_url)
    return TransportResponse(request_url, response_url, status, headers, body)


class MappingTransport:
    """确定性的站点映射，供离线演练和测试使用。

    键按 :func:`app.urls.normalize_url` 归一化后匹配。值可以是响应对象，也可以
    是 ``callable(url, headers) -> 响应对象``。所有请求保存在 ``requests`` 中，
    便于断言没有访问范围外地址。
    """

    def __init__(self, mapping):
        if not isinstance(mapping, dict):
            raise ValueError("站点映射必须是对象")
        self._routes = {}
        for url, entry in mapping.items():
            if not isinstance(url, str):
                raise ValueError("站点映射键必须是 URL")
            key = normalize_url(url)
            if key in self._routes:
                raise ValueError("站点映射包含重复 URL")
            if not isinstance(entry, dict) and not callable(entry):
                raise ValueError("站点映射值无效")
            self._routes[key] = entry
        self.requests = []

    @classmethod
    def from_file(cls, path):
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("routes"), dict):
            value = value["routes"]
        return cls(value)

    def fetch(self, url, *, headers=None, max_bytes=1048576):
        split_http_url(url)
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("读取预算无效")
        key = normalize_url(url)
        self.requests.append({"url": url, "normalized_url": key, "headers": dict(headers or {})})
        entry = self._routes.get(key)
        if entry is None:
            raise TransportError("unmapped", "映射中没有该 URL")
        if callable(entry):
            entry = entry(url, dict(headers or {}))
        return _response_from_spec(entry, url, max_bytes)


__all__ = [
    "HttpTransport",
    "MappingTransport",
    "TransportError",
    "TransportResponse",
    "NoRedirect",
]
