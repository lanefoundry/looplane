"""Web fetching and search tools with SSRF protection."""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from looplane.tooling.types import ToolExecutionError

_REQUEST_TIMEOUT = 10.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB
_DEFAULT_MAX_CHARS = 16_000
_MAX_MAX_CHARS = 32_000
_USER_AGENT = "Looplane/0.1 (coding-agent; +https://github.com/lanefoundry/looplane)"

_BLOCKED_SCHEMES = frozenset({"file", "ftp", "data", "javascript", "vbscript"})
_PRIVATE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\n{3,}")


def _check_url_safety(url: str, *, allow_private: bool = False) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        raise ToolExecutionError("url must include scheme and hostname")
    if parsed.scheme.lower() in _BLOCKED_SCHEMES:
        raise ToolExecutionError(f"blocked scheme: {parsed.scheme}")
    if parsed.scheme.lower() not in ("http", "https"):
        raise ToolExecutionError(f"unsupported scheme: {parsed.scheme}")
    if not parsed.hostname:
        raise ToolExecutionError("url must include a hostname")

    if not allow_private:
        hostname = parsed.hostname
        try:
            addr = ipaddress.ip_address(hostname)
        except ValueError:
            try:
                resolved = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
                for _, _, _, _, sockaddr in resolved:
                    addr = ipaddress.ip_address(sockaddr[0])
                    if any(addr in net for net in _PRIVATE_NETS):
                        raise ToolExecutionError(
                            f"blocked: {hostname} resolves to private address {addr}"
                        )
            except socket.gaierror:
                pass
        else:
            if any(addr in net for net in _PRIVATE_NETS):
                raise ToolExecutionError(f"blocked: {hostname} is a private address")
    return url


def _extract_text(html: str) -> str:
    """Best-effort HTML-to-text: try trafilatura, fall back to tag stripping."""
    try:
        import trafilatura

        result = trafilatura.extract(html, include_links=True, include_tables=True)
        if result:
            return result
    except ImportError:
        pass

    text = _TAG_RE.sub("", html)
    text = _WHITESPACE_RE.sub("\n\n", text)
    return text.strip()


@dataclass
class WebFetchResult:
    url: str
    status_code: int
    content: str
    truncated: bool = False


def web_fetch(
    url: str,
    *,
    selector: str | None = None,
    max_chars: int = _DEFAULT_MAX_CHARS,
    allow_private: bool = False,
) -> str:
    url = _check_url_safety(url, allow_private=allow_private)
    max_chars = min(max(max_chars, 1000), _MAX_MAX_CHARS)

    with httpx.Client(
        timeout=_REQUEST_TIMEOUT,
        follow_redirects=True,
        max_redirects=5,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        response = client.get(url)
        response.raise_for_status()

        content_bytes = response.content[:_MAX_RESPONSE_BYTES]
        html = content_bytes.decode(response.encoding or "utf-8", errors="replace")

    if selector:
        try:
            from lxml import etree
            from lxml.html import fromstring

            doc = fromstring(html)
            elements = doc.cssselect(selector)
            if elements:
                html = "\n".join(
                    etree.tostring(el, method="html", encoding="unicode") for el in elements
                )
            else:
                return f"No elements matched selector: {selector}"
        except ImportError:
            pass

    text = _extract_text(html)

    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + "\n\n[truncated]"

    header = f"# Fetched: {url}\n\n"
    return header + text


@dataclass
class WebSearchResult:
    title: str
    url: str
    snippet: str


@dataclass
class WebSearchConfig:
    provider: str = "duckduckgo"
    api_key: str | None = None


def web_search(
    query: str,
    *,
    max_results: int = 5,
    config: WebSearchConfig | None = None,
) -> str:
    if not query or not query.strip():
        raise ToolExecutionError("query must be non-empty")
    max_results = min(max(max_results, 1), 10)
    config = config or WebSearchConfig()

    results = _search_duckduckgo(query, max_results=max_results)

    if not results:
        return f"No results found for: {query}"

    lines = [f"# Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. **{r.title}**")
        lines.append(f"   {r.url}")
        lines.append(f"   {r.snippet}\n")
    return "\n".join(lines)


def _search_duckduckgo(query: str, *, max_results: int = 5) -> list[WebSearchResult]:
    try:
        from duckduckgo_search import DDGS
    except ImportError as exc:
        raise ToolExecutionError(
            "web_search requires the 'duckduckgo-search' package. "
            "Install with: pip install duckduckgo-search"
        ) from exc

    with DDGS() as ddgs:
        raw = list(ddgs.text(query, max_results=max_results))

    return [
        WebSearchResult(
            title=r.get("title", ""),
            url=r.get("href", ""),
            snippet=r.get("body", ""),
        )
        for r in raw
    ]


_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_HTTP_TIMEOUT = 30.0
_HTTP_MAX_TIMEOUT = 60.0


def http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout: float = _HTTP_TIMEOUT,
    max_output_chars: int = 16_000,
    allow_private: bool = False,
) -> str:
    method = method.upper()
    if method not in _HTTP_METHODS:
        allowed = ", ".join(sorted(_HTTP_METHODS))
        raise ToolExecutionError(f"unsupported method: {method}. Use one of: {allowed}")

    url = _check_url_safety(url, allow_private=allow_private)
    timeout = min(max(timeout, 1.0), _HTTP_MAX_TIMEOUT)

    req_headers = {"User-Agent": _USER_AGENT}
    if headers:
        req_headers.update(headers)

    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        max_redirects=5,
        headers=req_headers,
    ) as client:
        response = client.request(method, url, content=body)

    sections = [f"HTTP {response.status_code} {response.reason_phrase}"]

    resp_headers = "\n".join(f"  {k}: {v}" for k, v in response.headers.items())
    sections.append(f"Headers:\n{resp_headers}")

    resp_body = response.text[:_MAX_RESPONSE_BYTES]
    if len(resp_body) > max_output_chars:
        resp_body = resp_body[:max_output_chars] + "\n[truncated]"
    sections.append(f"Body:\n{resp_body}")

    return "\n\n".join(sections)
