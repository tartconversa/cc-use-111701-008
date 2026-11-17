"""二维码落地链路采集。

引擎按登记任务逐跳推进，并保留每一跳的：

- 请求 URL 与响应 URL
- HTTP 状态和响应头摘要（含 ``Content-Type``/``Location``）
- 跳转方式：HTTP 3xx、页面内 ``meta refresh``、脚本导航、DOM 自动导航
- 授权判断（命中的登记域名，或越界的完整目标）
- 实体摘要与最终内容身份

三类跳转可以交错出现，按实际顺序记录。以下情况立即停止并指出位置：

- 循环：目标规范化 URL 已在本链路出现过（包括页面刷新自身）
- 越界：目标主机不属于该二维码登记的任何域名，或协议不是 http/https
- 页数/跳数预算耗尽
- 页面存在无法静态解析或互相竞争的导航意图

停止时 ``outcome`` 不是 ``complete``，``failure`` 给出步骤、URL 和触发位置。
未完成的跳转不会被当成最终页面，跨站目标也不会被标成自家活动页。
"""
from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

from .htmlnav import NavigationSignal, analyze, parse_meta_refresh
from .snapshot import content_identity
from .urls import (
    URLPolicyError,
    authorize_url,
    normalize_url,
    resolve_url,
    split_http_url,
    strip_fragment,
)
from .transport import TransportError

MAX_HOPS = 12
MAX_PAGES = 8

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_TRANSITION_DESCRIPTIONS = {
    "http_redirect": "HTTP 3xx 跳转",
    "meta_refresh": "页面内 meta refresh",
    "script_navigation": "脚本导航",
    "dom_navigation": "DOM 自动导航",
}


def _sha256(body):
    return hashlib.sha256(body).hexdigest()


def _error(kind, message, **extra):
    value = {"kind": kind, "message": message}
    value.update(extra)
    return value


def _base_record(registration, step, url):
    return {
        "step": step,
        "request_url": url,
        "response_url": url,
        "status": None,
        "headers": [],
        "content_type": None,
        "transition": None,
        "authorization": None,
        "body_sha256": None,
        "content": None,
        "signals": [],
        "outcome": "pending",
        "stop_reason": None,
        "failure": None,
    }


def _finish_path(result):
    result["path"] = [
        hop.get("response_url") or hop.get("request_url") for hop in result["hops"]
    ]


def _stop(result, record, outcome, reason, failure):
    result["pending_hop"] = None
    record["outcome"] = outcome
    record["stop_reason"] = reason
    record["failure"] = failure
    result["hops"].append(record)
    result["outcome"] = outcome
    result["stop_reason"] = reason
    result["failure"] = failure
    _finish_path(result)
    return result


def _finish(result, record, content):
    result["pending_hop"] = None
    record["content"] = content
    record["outcome"] = "complete"
    result["hops"].append(record)
    result["outcome"] = "complete"
    result["final_content"] = content
    _finish_path(result)
    return result


class LandingCollector:
    """按单个登记任务采集落地链路。"""

    def __init__(self, transport, *, max_hops=MAX_HOPS, max_pages=MAX_PAGES):
        if type(max_hops) is not int or max_hops < 1:
            raise ValueError("跳数预算无效")
        if type(max_pages) is not int or max_pages < 1:
            raise ValueError("页面预算无效")
        if max_hops > 50 or max_pages > 30:
            raise ValueError("预算超出允许范围")
        self.transport = transport
        self.max_hops = max_hops
        self.max_pages = max_pages

    def collect(self, registration, *, scan_id=None):
        """采集一条链路，返回可序列化结果；任何失败都保留已核实的跳。"""
        result = {
            "scan_id": scan_id,
            "store_id": registration.store_id,
            "qr_id": registration.qr_id,
            "printed_url": registration.printed_url,
            "allowed_domains": list(registration.allowed_domains),
            "viewport": registration.viewport.as_dict(),
            "hops": [],
            "path": [],
            "outcome": "failed",
            "stop_reason": None,
            "failure": None,
            "final_content": None,
            "verified_hops": 0,
            "pending_hop": None,
        }
        url = registration.printed_url
        visited = {normalize_url(url)}
        pages = 0

        while True:
            step = len(result["hops"])
            if step >= self.max_hops:
                return self._budget(result, step, "hop_budget", "跳转超过预算", url)
            if pages >= self.max_pages:
                return self._budget(result, step, "page_budget", "页面数量超过预算", url)
            pages += 1
            record = _base_record(registration, step, url)
            ok, domain = authorize_url(url, registration.allowed_domains)
            record["authorization"] = {"allowed": ok, "matched_domain": domain}
            try:
                split_http_url(url)
            except URLPolicyError as exc:
                return _stop(
                    result, record, "blocked", "invalid_url",
                    _error("invalid_url", str(exc), step=step, url=url),
                )
            if not ok:
                return _stop(
                    result, record, "blocked", "out_of_scope",
                    _error(
                        "out_of_scope", "目标不在允许的域名内，未访问",
                        step=step, url=url,
                        allowed_domains=list(registration.allowed_domains),
                    ),
                )

            try:
                response = self.transport.fetch(
                    url,
                    headers=dict(registration.viewport.request_headers),
                    max_bytes=1048576,
                )
            except TransportError as exc:
                return _stop(
                    result, record, "error", "transport_error",
                    _error(exc.kind, exc.detail, step=step, url=url),
                )
            except ValueError as exc:
                return _stop(
                    result, record, "error", "transport_error",
                    _error("transport_error", str(exc), step=step, url=url),
                )

            record["response_url"] = response.response_url
            record["status"] = response.status
            record["headers"] = [list(pair) for pair in response.headers]
            record["content_type"] = response.content_type
            record["body_sha256"] = _sha256(response.body)
            result["verified_hops"] = step + 1

            # 传输层可能改写了地址（例如服务端返回了单一规范化地址）。
            # 改写到范围外时同样按越界停止，不继续解析实体。
            if normalize_url(response.response_url) != normalize_url(url):
                ok_final, domain = authorize_url(
                    response.response_url, registration.allowed_domains
                )
                if not ok_final:
                    record["authorization"] = {"allowed": False, "matched_domain": None}
                    return _stop(
                        result, record, "blocked", "out_of_scope",
                        _error(
                            "out_of_scope", "响应把页面带到允许范围外，未继续",
                            step=step, url=response.response_url,
                            from_url=url,
                            allowed_domains=list(registration.allowed_domains),
                        ),
                    )
                record["authorization"] = {"allowed": True, "matched_domain": domain}
                visited.add(normalize_url(response.response_url))

            next_url = self._decide(
                result, record, response, registration, step, visited
            )
            if next_url is None:
                return result
            # fragment 不会发给服务端，也不构成新的页面；按去掉 fragment 的
            # 地址继续，避免把同一次导航记成两跳。循环判断已在 _decide 中
            # 用规范化地址完成。
            url = strip_fragment(next_url)
            visited.add(normalize_url(url))

    # -- 单跳决策 -------------------------------------------------------
    def _decide(self, result, record, response, registration, step, visited):
        location = response.header("location")
        if location is not None and response.status not in REDIRECT_STATUSES:
            # Location 与状态不一致时，浏览器不会按跳转处理。把这一事实记进
            # 收据，避免后来的人把页面当成“没跳转”。
            record["ignored_location"] = location
        if response.status in REDIRECT_STATUSES:
            return self._http_redirect(result, record, response, registration, step, visited)
        if 300 <= response.status < 400:
            _stop(
                result, record, "unfinished", "unsupported_redirect",
                _error(
                    "unsupported_redirect", "不支持的 3xx 状态，无法判定跳转目标",
                    step=step, url=record["request_url"], status=response.status,
                ),
            )
            return None
        if response.status >= 400:
            _stop(
                result, record, "error", "http_error",
                _error(
                    "http_error", "来源返回错误状态",
                    step=step, url=record["request_url"], status=response.status,
                ),
            )
            return None
        if not 200 <= response.status < 300:
            _stop(
                result, record, "error", "unexpected_status",
                _error(
                    "unexpected_status", "无法处理的状态",
                    step=step, url=record["request_url"], status=response.status,
                ),
            )
            return None

        try:
            analysis = analyze(response.body, response.content_type, response.response_url)
        except ValueError as exc:
            _stop(
                result, record, "error", "parse_error",
                _error("parse_error", str(exc), step=step, url=record["request_url"]),
            )
            return None

        if not analysis.html:
            try:
                content = content_identity(response.body, response.headers, None)
            except ValueError as exc:
                _stop(
                    result, record, "error", "content_identity_invalid",
                    _error(
                        "content_identity_invalid", str(exc),
                        step=step, url=record["request_url"],
                    ),
                )
                return None
            _finish(result, record, content)
            return None

        refresh_header = response.header("refresh")
        if refresh_header:
            delay, target = parse_meta_refresh(refresh_header)
            if delay is not None or target is not None:
                analysis.signals.append(
                    NavigationSignal(
                        kind="meta_refresh",
                        target=target or response.response_url,
                        source=f"header:Refresh {refresh_header!r}",
                        location="response header",
                        delay_seconds=delay,
                    )
                )

        record["title"] = analysis.title
        record["document_base_url"] = analysis.base_url
        record["signals"] = [signal.as_dict() for signal in analysis.signals]
        if analysis.external_scripts:
            record["external_scripts"] = list(analysis.external_scripts)

        # 外部脚本无法静态判断是否还会导航；既然不能证明页面已加载完成，
        # 就把它报成未完成，而不是假定外部脚本不会跳转。
        if analysis.external_scripts and not analysis.signals:
            _stop(
                result, record, "unfinished", "external_script",
                _error(
                    "external_script",
                    "页面依赖外部脚本，无法静态确认加载完成",
                    step=step, url=record["response_url"],
                    scripts=list(analysis.external_scripts),
                ),
            )
            return None

        static = []
        dynamic = []
        invalid = []
        for signal in analysis.signals:
            if signal.dynamic:
                dynamic.append(signal)
                continue
            try:
                static.append((signal, resolve_url(analysis.base_url, signal.target)))
            except URLPolicyError as exc:
                invalid.append((signal, str(exc)))

        if invalid:
            signal, detail = invalid[0]
            _stop(
                result, record, "unfinished", "invalid_navigation",
                _error(
                    "invalid_navigation", "页面中的跳转目标无法解析",
                    step=step, url=record["response_url"],
                    signal=signal.as_dict(), detail=detail,
                ),
            )
            return None
        if dynamic or len({target for _, target in static}) > 1:
            _stop(
                result, record, "unfinished", "ambiguous_navigation",
                _error(
                    "ambiguous_navigation",
                    "页面同时存在多个或无法静态确定的跳转意图，不能假定已加载完成",
                    step=step, url=record["response_url"],
                    targets=sorted({target for _, target in static}),
                    dynamic=bool(dynamic),
                    signals=[signal.as_dict() for signal in analysis.signals],
                ),
            )
            return None

        if not static:
            try:
                content = content_identity(response.body, response.headers, analysis)
            except ValueError as exc:
                _stop(
                    result, record, "error", "content_identity_invalid",
                    _error(
                        "content_identity_invalid", str(exc),
                        step=step, url=record["request_url"],
                    ),
                )
                return None
            _finish(result, record, content)
            return None

        signal, target = static[0]
        ok, matched = authorize_url(target, registration.allowed_domains)
        record["transition"] = {
            "kind": signal.kind,
            "description": _TRANSITION_DESCRIPTIONS[signal.kind],
            "from_url": record["response_url"],
            "to_url": target,
            "target_host": urlsplit(target).hostname,
            "authorized": ok,
            "matched_domain": matched,
            "signal": signal.as_dict(),
        }
        if not ok:
            _stop(
                result, record, "blocked", "out_of_scope",
                _error(
                    "out_of_scope", "页面内跳转指向允许范围外，已停止",
                    step=step, url=target, from_url=record["response_url"],
                    signal=signal.as_dict(),
                    allowed_domains=list(registration.allowed_domains),
                ),
            )
            return None
        if normalize_url(target) in visited:
            _stop(
                result, record, "stopped", "navigation_loop",
                _error(
                    "navigation_loop", "跳转回到已访问地址",
                    step=step, url=target, from_url=record["response_url"],
                    signal=signal.as_dict(),
                ),
            )
            return None
        result["hops"].append(record)
        result["pending_hop"] = {
            "from_step": step,
            "to_url": target,
            "kind": signal.kind,
            "signal": signal.as_dict(),
        }
        return target

    def _http_redirect(self, result, record, response, registration, step, visited):
        location = response.header("location")
        if location is None or not location.strip():
            _stop(
                result, record, "unfinished", "missing_location",
                _error(
                    "missing_location", "3xx 响应缺少 Location",
                    step=step, url=record["request_url"], status=response.status,
                ),
            )
            return None
        try:
            target = resolve_url(response.response_url, location)
        except URLPolicyError as exc:
            _stop(
                result, record, "unfinished", "invalid_navigation",
                _error(
                    "invalid_navigation", "Location 无法解析",
                    step=step, url=record["request_url"],
                    status=response.status, detail=str(exc),
                ),
            )
            return None
        ok, matched = authorize_url(target, registration.allowed_domains)
        record["transition"] = {
            "kind": "http_redirect",
            "description": _TRANSITION_DESCRIPTIONS["http_redirect"],
            "from_url": response.response_url,
            "to_url": target,
            "target_host": urlsplit(target).hostname,
            "authorized": ok,
            "matched_domain": matched,
            "status": response.status,
            "location": location,
        }
        if not ok:
            _stop(
                result, record, "blocked", "out_of_scope",
                _error(
                    "out_of_scope", "HTTP 跳转指向允许范围外，已停止",
                    step=step, url=target, from_url=response.response_url,
                    status=response.status, location=location,
                    allowed_domains=list(registration.allowed_domains),
                ),
            )
            return None
        if normalize_url(target) in visited:
            _stop(
                result, record, "stopped", "redirect_loop",
                _error(
                    "redirect_loop", "HTTP 跳转回到已访问地址",
                    step=step, url=target, from_url=response.response_url,
                    status=response.status, location=location,
                ),
            )
            return None
        result["hops"].append(record)
        result["pending_hop"] = {
            "from_step": step,
            "to_url": target,
            "kind": "http_redirect",
            "status": response.status,
            "location": location,
        }
        return target

    def _budget(self, result, step, kind, message, url):
        result["pending_hop"] = None
        result["outcome"] = "unfinished"
        result["stop_reason"] = kind
        result["failure"] = _error(kind, message, step=step, url=url)
        _finish_path(result)
        return result


def collect_landing(transport, registration, *, scan_id=None, max_hops=MAX_HOPS, max_pages=MAX_PAGES):
    return LandingCollector(transport, max_hops=max_hops, max_pages=max_pages).collect(
        registration, scan_id=scan_id
    )
