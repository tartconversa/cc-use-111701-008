"""URL 规范化与授权域名判断。

采集器只处理 http/https 页面。主机名比较不区分大小写；登记的域名既匹配
自身，也匹配其子域，但不会把 ``evil-example.test`` 误认为 ``example.test``
的子域。
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit


class URLPolicyError(ValueError):
    """URL 不符合采集策略。"""


def _host_for_compare(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    if not host:
        raise URLPolicyError("URL 缺少主机名")
    try:
        # 统一 Unicode 主机名，避免同一个站点出现两种写法。
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise URLPolicyError("URL 主机名无法规范化") from exc
    return host


def split_http_url(url: str):
    """拆分并校验一个绝对 http/https URL。"""
    if not isinstance(url, str) or not url.strip():
        raise URLPolicyError("URL 为空")
    if any(ch.isspace() for ch in url):
        raise URLPolicyError("URL 含空白字符")
    if "\\" in url:
        # 浏览器在特殊 scheme 下可能把反斜杠当作斜杠，静态解析与真实行为
        # 会分歧，因此直接拒绝。
        raise URLPolicyError("URL 含反斜杠")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https"):
        raise URLPolicyError("只允许 http/https URL")
    if parts.username is not None or parts.password is not None:
        raise URLPolicyError("URL 不允许携带用户凭据")
    if not parts.hostname:
        raise URLPolicyError("URL 缺少主机名")
    if "%" in parts.netloc:
        raise URLPolicyError("URL 主机名不允许百分号编码")
    try:
        parts.port  # noqa: B018 - 触发端口校验
    except ValueError as exc:
        raise URLPolicyError("URL 端口无效") from exc
    _host_for_compare(parts.hostname)
    return parts


def normalize_url(url: str) -> str:
    """用于比较和循环判断的规范化 URL。

    - scheme/host 小写
    - 去掉默认端口和 fragment
    - 空路径补成 ``/``
    - 保留路径与查询参数的原始顺序（参数顺序可能影响服务端行为）
    """
    parts = split_http_url(url)
    scheme = parts.scheme.lower()
    host = _host_for_compare(parts.hostname)
    port = parts.port
    netloc = host
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def canonical_host(host: str) -> str:
    return _host_for_compare(host)


def is_ip_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip().strip("[]"))
        return True
    except ValueError:
        return False


def domain_matches(host: str, allowed_domain: str) -> bool:
    """判断 host 是否属于 allowed_domain。

    登记 ``example.test`` 时允许 ``example.test`` 和 ``a.example.test``；
    不允许 ``badexample.test``。登记 ``*.example.test`` 时只允许子域。
    """
    host = canonical_host(host)
    domain = allowed_domain.strip().lower().rstrip(".")
    if not domain:
        return False
    wildcard = domain.startswith("*.")
    if wildcard:
        domain = domain[2:]
    if not domain or any(ch in domain for ch in "/:@?#[]"):
        return False
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return False
    if is_ip_host(domain) or is_ip_host(host):
        return host == domain
    if wildcard:
        return host.endswith("." + domain)
    return host == domain or host.endswith("." + domain)


def authorize_url(url: str, allowed_domains) -> tuple[bool, str | None]:
    """返回 ``(是否授权, 命中的登记域名)``。

    URL 本身不合法时返回 ``(False, None)``；调用方应根据完整 URL 判断是
    跨站还是协议不支持。
    """
    try:
        parts = split_http_url(url)
    except URLPolicyError:
        return False, None
    host = _host_for_compare(parts.hostname)
    for domain in allowed_domains:
        if domain_matches(host, domain):
            return True, domain.strip().lower().rstrip(".")
    return False, None


def resolve_url(base: str, reference: str) -> str:
    """把页面中的相对/绝对 URL 解析成绝对 URL。"""
    from urllib.parse import urljoin

    if reference is None:
        raise URLPolicyError("跳转目标为空")
    reference = reference.strip()
    if not reference:
        raise URLPolicyError("跳转目标为空")
    target = urljoin(base, reference)
    split_http_url(target)
    return target


def strip_fragment(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
