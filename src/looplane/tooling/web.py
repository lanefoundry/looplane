"""Web fetching and search tools with SSRF protection."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

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


# ===================================================================
# Search Provider System
# ===================================================================
#
# Fallback chain inspired by omp (oh-my-pi) and opencode:
#   API-key providers first → credential-free providers as fallback.
#   Default order: tavily → brave → jina → exa → parallel → duckduckgo
#
# API-key providers activate only when their env var is set.
# Credential-free providers are always available.
# ===================================================================

_SEARCH_TIMEOUT = 25.0

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class _SearchProvider:
    name: str = ""

    def is_available(self) -> bool:
        return True

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
    ) -> list[WebSearchResult]:
        raise NotImplementedError


# -------------------------------------------------------------------
# API-key providers (activate when env var is set)
# -------------------------------------------------------------------


class _TavilySearch(_SearchProvider):
    name = "tavily"

    def is_available(self) -> bool:
        return bool(os.environ.get("TAVILY_API_KEY"))

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
    ) -> list[WebSearchResult]:
        api_key = os.environ["TAVILY_API_KEY"]
        with httpx.Client(timeout=_SEARCH_TIMEOUT) as client:
            resp = client.post(
                "https://api.tavily.com/search",
                json={
                    "query": query,
                    "search_depth": "basic",
                    "max_results": max_results,
                    "include_answer": False,
                    "include_raw_content": False,
                },
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            resp.raise_for_status()
        data = resp.json()
        return [
            WebSearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content", ""),
            )
            for r in data.get("results", [])
        ]


class _BraveSearch(_SearchProvider):
    name = "brave"

    def is_available(self) -> bool:
        return bool(os.environ.get("BRAVE_API_KEY"))

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
    ) -> list[WebSearchResult]:
        api_key = os.environ["BRAVE_API_KEY"]
        with httpx.Client(timeout=_SEARCH_TIMEOUT) as client:
            resp = client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={
                    "q": query,
                    "count": max_results,
                    "extra_snippets": "true",
                    "text_decorations": "false",
                    "safesearch": "moderate",
                },
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": api_key,
                },
            )
            resp.raise_for_status()
        data = resp.json()
        return [
            WebSearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("description", ""),
            )
            for r in data.get("web", {}).get("results", [])
        ]


class _JinaSearch(_SearchProvider):
    name = "jina"

    def is_available(self) -> bool:
        return bool(os.environ.get("JINA_API_KEY"))

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
    ) -> list[WebSearchResult]:
        api_key = os.environ["JINA_API_KEY"]
        with httpx.Client(timeout=_SEARCH_TIMEOUT) as client:
            resp = client.get(
                f"https://s.jina.ai/{quote(query)}",
                params={"count": max_results},
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "X-Respond-With": "no-content",
                    "X-Retain-Images": "none",
                },
            )
            resp.raise_for_status()
        data = resp.json()
        items = data if isinstance(data, list) else data.get("data", [])
        return [
            WebSearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=(r.get("description", "") or r.get("content", "")[:500]),
            )
            for r in items
        ]


# -------------------------------------------------------------------
# Credential-free providers (always available)
# -------------------------------------------------------------------


class _ExaSearch(_SearchProvider):
    name = "exa"

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
    ) -> list[WebSearchResult]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "web_search_exa",
                "arguments": {
                    "query": query,
                    "type": "auto",
                    "numResults": max_results,
                    "livecrawl": "fallback",
                },
            },
        }
        with httpx.Client(timeout=_SEARCH_TIMEOUT) as client:
            resp = client.post(
                "https://mcp.exa.ai/mcp",
                json=payload,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
        text = _parse_mcp_response(resp.text)
        return _parse_exa_text(text)


class _ParallelSearch(_SearchProvider):
    name = "parallel"

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
    ) -> list[WebSearchResult]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "web_search",
                "arguments": {
                    "objective": query,
                    "search_queries": [query],
                    "mode": "fast",
                    "max_results": max_results,
                },
            },
        }
        with httpx.Client(timeout=_SEARCH_TIMEOUT) as client:
            resp = client.post(
                "https://search.parallel.ai/mcp",
                json=payload,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
        text = _parse_mcp_response(resp.text)
        return _parse_parallel_text(text)


def _parse_parallel_text(text: str) -> list[WebSearchResult]:
    """Parse Parallel's JSON response embedded in MCP text."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _parse_exa_text(text)
    raw = data.get("results", [])
    return [
        WebSearchResult(
            title=r.get("title", ""),
            url=r.get("url", ""),
            snippet=" ".join(r.get("excerpts", [])),
        )
        for r in raw
        if r.get("url")
    ]


_DDG_RESULT_RE = re.compile(
    r'<div[^>]+class="[^"]*result\b[^"]*"[^>]*>(.*?)</div>'
    r'\s*(?=<div[^>]+class="[^"]*result\b|$)',
    re.DOTALL,
)
_DDG_TITLE_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]*href="([^"]*)"[^>]*>'
    r"(.*?)</a>",
    re.DOTALL,
)
_DDG_SNIPPET_RE = re.compile(
    r'class="result__snippet"[^>]*>(.*?)</(?:a|div|span)>',
    re.DOTALL,
)
_DDG_UDDG_RE = re.compile(r"[?&]uddg=([^&]+)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&#(\d+);")
_NAMED_ENTITY = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&apos;": "'",
}


class _DuckDuckGoSearch(_SearchProvider):
    name = "duckduckgo"

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
    ) -> list[WebSearchResult]:
        with httpx.Client(
            timeout=_SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": _BROWSER_UA,
                "Referer": "https://html.duckduckgo.com/",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            resp = client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query, "kl": "us-en", "b": "", "df": ""},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            resp.raise_for_status()

        html = resp.text
        if "anomaly-modal" in html or "anomaly.js" in html:
            raise ToolExecutionError("DuckDuckGo bot detection triggered")

        results: list[WebSearchResult] = []
        for match in _DDG_RESULT_RE.finditer(html):
            if len(results) >= max_results:
                break
            block = match.group(1)

            title_m = _DDG_TITLE_RE.search(block)
            if not title_m:
                continue
            raw_href, raw_title = title_m.group(1), title_m.group(2)

            uddg_m = _DDG_UDDG_RE.search(raw_href)
            url = unquote(uddg_m.group(1)) if uddg_m else raw_href
            if not url or url.startswith("//duckduckgo.com"):
                continue

            title = _decode_html(raw_title)
            snippet_m = _DDG_SNIPPET_RE.search(block)
            snippet = _decode_html(snippet_m.group(1)) if snippet_m else ""
            results.append(
                WebSearchResult(title=title, url=url, snippet=snippet),
            )
        return results


# -------------------------------------------------------------------
# Shared helpers
# -------------------------------------------------------------------


def _parse_mcp_response(body: str) -> str:
    """Extract text content from a JSON-RPC or SSE MCP response."""
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                obj = json.loads(line[6:])
                content = obj.get("result", {}).get("content", [])
                for item in content:
                    if item.get("type") == "text":
                        return item["text"]
            except (json.JSONDecodeError, KeyError):
                continue

    obj = json.loads(body)
    if "error" in obj:
        raise ToolExecutionError(f"MCP error: {obj['error']}")
    content = obj.get("result", {}).get("content", [])
    for item in content:
        if item.get("type") == "text":
            return item["text"]
    raise ToolExecutionError("MCP returned no text content")


def _parse_exa_text(text: str) -> list[WebSearchResult]:
    """Parse Title:/URL: text blocks into results."""
    results: list[WebSearchResult] = []
    title = url = snippet = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Title:"):
            if url:
                results.append(
                    WebSearchResult(
                        title=title,
                        url=url,
                        snippet=snippet,
                    ),
                )
            title = line[6:].strip()
            url = snippet = ""
        elif line.startswith("URL:"):
            url = line[4:].strip()
        elif line.startswith(("ID:", "Score:", "Published Date:")):
            continue
        elif line and url:
            snippet = (snippet + " " + line).strip() if snippet else line
    if url:
        results.append(
            WebSearchResult(title=title, url=url, snippet=snippet),
        )
    return results


def _decode_html(s: str) -> str:
    """Strip HTML tags and decode entities."""
    text = _HTML_TAG_RE.sub("", s)
    text = _HTML_ENTITY_RE.sub(lambda m: chr(int(m.group(1))), text)
    for entity, char in _NAMED_ENTITY.items():
        text = text.replace(entity, char)
    return text.strip()


# -------------------------------------------------------------------
# Chain and public API
# -------------------------------------------------------------------

_DEFAULT_CHAIN: list[_SearchProvider] = [
    _TavilySearch(),
    _BraveSearch(),
    _JinaSearch(),
    _ExaSearch(),
    _ParallelSearch(),
    _DuckDuckGoSearch(),
]


def web_search(
    query: str,
    *,
    max_results: int = 5,
) -> str:
    if not query or not query.strip():
        raise ToolExecutionError("query must be non-empty")
    max_results = min(max(max_results, 1), 10)

    errors: list[str] = []
    for provider in _DEFAULT_CHAIN:
        if not provider.is_available():
            continue
        try:
            results = provider.search(query, max_results=max_results)
            if results:
                lines = [f"# Search results for: {query}\n"]
                for i, r in enumerate(results, 1):
                    lines.append(f"{i}. **{r.title}**")
                    lines.append(f"   {r.url}")
                    lines.append(f"   {r.snippet}\n")
                return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{provider.name}: {exc}")

    if errors:
        detail = "\n".join(errors)
        return f"No results found for: {query}\n\nBackend errors:\n{detail}"
    return f"No results found for: {query}"


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
