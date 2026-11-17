"""HTML 文档的静态导航分析。

演练站点通过三类方式导航：HTTP 3xx（由链路引擎处理）、页面内 ``meta
refresh``、脚本生成的位置跳转，以及自动触发的 DOM 点击。本模块只做静态、
确定性的解析：

- 不执行脚本，只提取可判定的跳转目标；目标由变量拼接时标记为动态。
- 不等待任何固定时间，``meta refresh`` 与 ``setTimeout`` 的声明延时会原样
  记录，但不会据此阻塞。
- 页面标题只作为元数据，不参与“是否加载完毕”的判断。

加载完毕的判据是“文档中没有待执行的导航意图”。存在无法解析或互相冲突的
导航意图时，由链路引擎报告未完成，而不是把页面当作最终内容。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin


@dataclass(frozen=True)
class NavigationSignal:
    kind: str
    target: str | None
    source: str
    location: str
    delay_seconds: float | None = None
    dynamic: bool = False

    def as_dict(self):
        value = {
            "kind": self.kind,
            "source": self.source,
            "location": self.location,
            "target": self.target,
        }
        if self.delay_seconds is not None:
            value["declared_delay_seconds"] = self.delay_seconds
        if self.dynamic:
            value["dynamic"] = True
        return value


@dataclass
class DocumentAnalysis:
    base_url: str
    title: str | None = None
    explicit_content_id: str | None = None
    content_id_source: str | None = None
    signals: list[NavigationSignal] = field(default_factory=list)
    external_scripts: list[dict] = field(default_factory=list)
    html: bool = True


_META_CONTENT_ID_NAMES = {
    "content-id",
    "content_id",
    "campaign-id",
    "campaign_id",
}
_TITLE_MAX = 300


def _decode(body, content_type):
    charset = None
    if content_type:
        match = re.search(r"charset\s*=\s*\"?([\w.+-]+)", content_type, re.I)
        if match:
            charset = match.group(1)
    if not charset:
        match = re.search(rb"<meta[^>]+charset\s*=\s*[\"']?([\w.+-]+)", body[:4096], re.I)
        if match:
            charset = match.group(1).decode("ascii", "ignore")
    for candidate in (charset, "utf-8"):
        if not candidate:
            continue
        try:
            return body.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


def _parse_delay(value):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return None
    if delay < 0:
        return None
    return delay


def parse_meta_refresh(content):
    """解析 meta refresh 的 content，返回 ``(delay, target_or_None)``。

    没有 URL 部分表示刷新当前页；返回 ``target=None`` 并由调用方补成当前页
    URL，这样自刷新会被当作循环而不是“加载完成”。
    """
    if not isinstance(content, str):
        return None, None
    text = content.strip()
    if not text:
        return None, None
    parts = re.split(r"[;,]", text, maxsplit=1)
    delay = _parse_delay(parts[0].strip())
    rest = parts[1].strip() if len(parts) > 1 else ""
    if rest:
        match = re.match(r"url\s*=\s*(.*)$", rest, re.I | re.S)
        if match:
            rest = match.group(1).strip()
        if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "'\"":
            rest = rest[1:-1].strip()
    return delay, (rest or None)


_JS_STRING = r"(['\"`])(.*?)\1"
_JS_LOCATION_ASSIGN = re.compile(
    r"(?<![\w$])(?:(?:window|document|self|top|parent)\s*\.\s*)?location"
    r"(?:\s*\.\s*href)?\s*=(?!=)\s*" + _JS_STRING,
    re.S,
)
_JS_LOCATION_CALL = re.compile(
    r"(?<![\w$])(?:(?:window|document|self|top|parent)\s*\.\s*)?location"
    r"\s*\.\s*(?:replace|assign)\s*\(\s*" + _JS_STRING,
    re.S,
)
_JS_LOCATION_DYNAMIC = re.compile(
    r"(?<![\w$])(?:(?:window|document|self|top|parent)\s*\.\s*)?location"
    r"(?:\s*\.\s*href)?\s*=(?!=)\s*([A-Za-z_$][\w$]*(?:\s*[+.]\s*[^;\n]+)?)",
    re.S,
)
_JS_LOCATION_CALL_DYNAMIC = re.compile(
    r"(?<![\w$])(?:(?:window|document|self|top|parent)\s*\.\s*)?location"
    r"\s*\.\s*(?:replace|assign)\s*\(\s*([A-Za-z_$][\w$]*)",
    re.S,
)
_JS_TIMEOUT_BLOCK = re.compile(
    r"setTimeout\s*\(\s*(?:function\s*\([^)]*\)\s*\{|\([^)]*\)\s*=>\s*\{|"
    r"[A-Za-z_$][\w$]*\s*=>\s*\{)(.*?)\}\s*(?:,\s*(\d+(?:\.\d+)?))?\s*\)",
    re.S,
)
_JS_LOCATION_RELOAD = re.compile(
    r"(?<![\w$])(?:(?:window|document|self|top|parent)\s*\.\s*)?location"
    r"\s*\.\s*reload\s*\(\s*\)",
    re.S,
)
_JS_WINDOW_OPEN = re.compile(
    r"(?<![\w$])(?:window\s*\.\s*)?open\s*\(\s*" + _JS_STRING,
    re.S,
)
_JS_WINDOW_OPEN_DYNAMIC = re.compile(
    r"(?<![\w$])(?:window\s*\.\s*)?open\s*\(\s*([A-Za-z_$][\w$]*)",
    re.S,
)
_JS_FORM_SUBMIT = re.compile(
    r"(?<![\w$])(?:document\s*\.\s*)?(?:forms\s*\[[^\]]*\]|[A-Za-z_$][\w$]*)"
    r"\s*\.\s*submit\s*\(\s*\)",
    re.S,
)
_AUTO_EVENT_ATTRIBUTES = ("onload", "onpageshow")
_JS_GET_BY_ID_CLICK = re.compile(
    r"document\s*\.\s*getElementById\s*\(\s*" + _JS_STRING + r"\s*\)\s*\.\s*click\s*\(\s*\)",
    re.S,
)
_JS_QUERY_CLICK = re.compile(
    r"document\s*\.\s*querySelector\s*\(\s*" + _JS_STRING + r"\s*\)\s*\.\s*click\s*\(\s*\)",
    re.S,
)


def _line_of(text, index):
    return text.count("\n", 0, index) + 1


def _strip_js_comments(text):
    """去掉脚本注释并保留换行（行号不变）。

    只识别字符串字面量之外的注释，因此 ``'https://…'`` 中的 ``//`` 不会被
    截断。模板字符串按普通字符串处理，这是静态分析的已知近似。
    """
    out = []
    index = 0
    length = len(text)
    quote = None
    while index < length:
        char = text[index]
        if quote is not None:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"', "`"):
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length:
            following = text[index + 1]
            if following == "/":
                end = text.find("\n", index)
                if end == -1:
                    break
                index = end
                continue
            if following == "*":
                end = text.find("*/", index + 2)
                end = length if end == -1 else end + 2
                out.append("\n" * text.count("\n", index, end))
                index = end
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _script_type_executable(attrs):
    value = (attrs.get("type") or "").strip().lower()
    return value in ("", "text/javascript", "application/javascript", "module", "text/ecmascript")


class _Parser(HTMLParser):
    def __init__(self, document_url):
        super().__init__(convert_charrefs=True)
        self.document_url = document_url
        self.base_url = document_url
        self.base_seen = False
        self.title_parts = []
        self.in_title = False
        self.explicit_content_id = None
        self.content_id_source = None
        self.signals = []
        self._noscript_depth = 0
        self._template_depth = 0
        self._script = None
        self._script_index = 0
        self._ids = {}
        self._links = {}
        self._element_index = 0
        self.external_scripts = []

    # -- helpers ---------------------------------------------------------
    def _line(self):
        line, _ = self.getpos()
        return line

    def _add_signal(self, kind, target, source, line, delay=None, dynamic=False):
        self.signals.append(
            NavigationSignal(
                kind=kind,
                target=target,
                source=source,
                location=f"line {line}",
                delay_seconds=delay,
                dynamic=dynamic,
            )
        )

    def _resolve(self, reference):
        if reference is None:
            return None
        reference = reference.strip()
        if not reference:
            return None
        return urljoin(self.base_url, reference)

    # -- HTMLParser hooks -------------------------------------------------
    def handle_starttag(self, tag, attrs_list):
        tag = tag.lower()
        attrs = {}
        for key, value in attrs_list:
            attrs.setdefault(key.lower(), value if value is not None else "")
        self._element_index += 1
        if tag == "noscript":
            self._noscript_depth += 1
            return
        if tag == "template":
            self._template_depth += 1
            return
        if self._noscript_depth or self._template_depth:
            return
        if tag == "base" and not self.base_seen:
            href = attrs.get("href")
            if href:
                resolved = self._resolve(href)
                if resolved:
                    self.base_url = resolved
                    self.base_seen = True
        elif tag == "title":
            self.in_title = True
        elif tag == "meta":
            http_equiv = (attrs.get("http-equiv") or "").strip().lower()
            name = (attrs.get("name") or attrs.get("property") or "").strip().lower()
            content = attrs.get("content") or ""
            if http_equiv == "refresh":
                delay, target = parse_meta_refresh(content)
                if delay is None and target is None:
                    return
                if target is None:
                    # 刷新当前页：用当前文档 URL 表达，便于循环检测。
                    target = self.document_url
                self._add_signal(
                    "meta_refresh",
                    target,
                    f"meta http-equiv=refresh content={content!r}",
                    self._line(),
                    delay=delay,
                )
            elif name in _META_CONTENT_ID_NAMES and content.strip() and self.explicit_content_id is None:
                self.explicit_content_id = content.strip()
                self.content_id_source = f"meta[name={name}]"
        elif tag in ("a", "area", "form"):
            href = attrs.get("href") if tag != "form" else attrs.get("action")
            element_id = attrs.get("id")
            if element_id:
                self._ids[element_id] = (tag, href, self._line())
            is_auto = (
                "data-auto-navigate" in attrs
                or (attrs.get("data-nav") or "").strip().lower() == "auto"
            )
            if tag != "form" and href and is_auto:
                self._add_signal(
                    "dom_navigation",
                    href,
                    f"<{tag} data-auto-navigate href={href!r}>",
                    self._line(),
                )
            if href:
                self._links.setdefault((tag, href), self._line())
        elif tag == "body":
            value = attrs.get("data-content-id")
            if value and self.explicit_content_id is None:
                self.explicit_content_id = value.strip()
                self.content_id_source = "body[data-content-id]"
        elif tag == "script":
            if _script_type_executable(attrs):
                src = (attrs.get("src") or "").strip()
                self._script = {
                    "index": self._script_index,
                    "line": self._line(),
                    "text": [],
                    "src": self._resolve(src) if src else None,
                }
                self._script_index += 1
        for name in _AUTO_EVENT_ATTRIBUTES:
            handler = attrs.get(name)
            if handler and handler.strip():
                self._event_signals(name, handler, self._line())

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "noscript":
            self._noscript_depth = max(0, self._noscript_depth - 1)
        elif tag == "template":
            self._template_depth = max(0, self._template_depth - 1)
        elif tag == "title":
            self.in_title = False
        elif tag == "script" and self._script is not None:
            self._finish_script(self._script)
            self._script = None

    def _event_signals(self, name, handler, line):
        before = len(self.signals)
        self._scan_js(handler, line)
        for signal in self.signals[before:]:
            self.signals[self.signals.index(signal)] = NavigationSignal(
                kind=signal.kind,
                target=signal.target,
                source=f"<{name}> {signal.source}",
                location=signal.location,
                delay_seconds=signal.delay_seconds,
                dynamic=signal.dynamic,
            )

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if self._script is not None:
            self._script["text"].append(data)

    def _scan_js(self, text, line):
        text = _strip_js_comments(text)
        seen = set()

        def add(target, snippet, signal_line, delay=None, dynamic=False, kind="script_navigation"):
            key = (kind, target, dynamic)
            if key in seen:
                return
            seen.add(key)
            self._add_signal(
                kind,
                target,
                snippet.strip()[:200],
                signal_line,
                delay=delay,
                dynamic=dynamic,
            )

        # 先记录 setTimeout 区间：只读声明延时，不实际等待。
        timeouts = []
        for match in _JS_TIMEOUT_BLOCK.finditer(text):
            timeouts.append((match.start(1), match.end(1), _parse_delay(match.group(2))))

        def delay_for(position):
            for low, high, delay in timeouts:
                if low <= position < high:
                    return delay
            return None

        def is_concatenated(match):
            return bool(re.match(r"\s*\+", text[match.end():]))

        for pattern in (_JS_LOCATION_ASSIGN, _JS_LOCATION_CALL):
            for match in pattern.finditer(text):
                position = match.start()
                signal_line = line + _line_of(text, position) - 1
                if is_concatenated(match):
                    add(None, match.group(0), signal_line, dynamic=True)
                else:
                    add(match.group(2), match.group(0), signal_line, delay=delay_for(position))
        for pattern in (_JS_LOCATION_DYNAMIC, _JS_LOCATION_CALL_DYNAMIC):
            for match in pattern.finditer(text):
                signal_line = line + _line_of(text, match.start()) - 1
                add(None, match.group(0), signal_line, dynamic=True)
        for match in _JS_WINDOW_OPEN.finditer(text):
            signal_line = line + _line_of(text, match.start()) - 1
            if is_concatenated(match):
                add(None, match.group(0), signal_line, dynamic=True)
            else:
                add(match.group(2), match.group(0), signal_line, delay=delay_for(match.start()))
        for match in _JS_WINDOW_OPEN_DYNAMIC.finditer(text):
            signal_line = line + _line_of(text, match.start()) - 1
            add(None, match.group(0), signal_line, dynamic=True)
        for match in _JS_FORM_SUBMIT.finditer(text):
            signal_line = line + _line_of(text, match.start()) - 1
            add(None, match.group(0), signal_line, dynamic=True)
        for match in _JS_GET_BY_ID_CLICK.finditer(text):
            element_id = match.group(2)
            found = self._ids.get(element_id)
            signal_line = line + _line_of(text, match.start()) - 1
            if found and found[1]:
                add(
                    found[1],
                    f"getElementById({element_id!r}).click() -> <{found[0]} href={found[1]!r}>",
                    signal_line,
                    kind="dom_navigation",
                )
            else:
                add(None, match.group(0), signal_line, dynamic=True)
        for match in _JS_QUERY_CLICK.finditer(text):
            signal_line = line + _line_of(text, match.start()) - 1
            add(None, match.group(0), signal_line, dynamic=True)
        for match in _JS_LOCATION_RELOAD.finditer(text):
            signal_line = line + _line_of(text, match.start()) - 1
            add(self.document_url, match.group(0), signal_line)

    def _finish_script(self, script):
        if script["src"]:
            # 外部脚本无法静态分析：不解析其（并不存在的）内联代码，
            # 只登记为未核实的脚本来源。
            self.external_scripts.append(
                {"src": script["src"], "location": f"line {script['line']}"}
            )
            return
        self._scan_js("".join(script["text"]), script["line"])


def analyze(body, content_type, document_url):
    """分析文档，返回 :class:`DocumentAnalysis`。"""
    if not isinstance(body, (bytes, bytearray)):
        raise ValueError("文档实体必须是字节")
    is_html = True
    if content_type and "html" not in content_type.lower():
        is_html = "xhtml" in content_type.lower()
    if not is_html:
        return DocumentAnalysis(base_url=document_url, html=False)
    text = _decode(bytes(body), content_type)
    if not re.search(r"<[A-Za-z!/]", text):
        return DocumentAnalysis(base_url=document_url, html=False)
    parser = _Parser(document_url)
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # HTMLParser 对损坏文档通常不抛错；保留防御
        raise ValueError("HTML 文档无法解析") from exc
    title = " ".join("".join(parser.title_parts).split())[:_TITLE_MAX] or None
    resolved = []
    for signal in parser.signals:
        if signal.target is not None and not signal.dynamic:
            resolved.append(
                NavigationSignal(
                    kind=signal.kind,
                    target=parser._resolve(signal.target),
                    source=signal.source,
                    location=signal.location,
                    delay_seconds=signal.delay_seconds,
                    dynamic=signal.dynamic,
                )
            )
        else:
            resolved.append(signal)
    return DocumentAnalysis(
        base_url=parser.base_url,
        external_scripts=parser.external_scripts,
        title=title,
        explicit_content_id=parser.explicit_content_id,
        content_id_source=parser.content_id_source,
        signals=resolved,
        html=True,
    )
