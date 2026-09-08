"""Tests for tooling.web — web_fetch, web_search, http_request."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from looplane.tooling.types import ToolExecutionError
from looplane.tooling.web import (
    _check_url_safety,
    http_request,
    web_fetch,
    web_search,
)


class TestUrlSafety:
    def test_blocks_file_scheme(self):
        with pytest.raises(ToolExecutionError, match="blocked scheme"):
            _check_url_safety("file:///etc/passwd")

    def test_blocks_data_scheme(self):
        with pytest.raises(ToolExecutionError, match="blocked scheme"):
            _check_url_safety("data:text/html,<h1>hi</h1>")

    def test_blocks_no_scheme(self):
        with pytest.raises(ToolExecutionError, match="must include scheme"):
            _check_url_safety("example.com")

    def test_blocks_localhost(self):
        with pytest.raises(ToolExecutionError, match="private address"):
            _check_url_safety("http://127.0.0.1/admin")

    def test_blocks_private_ip(self):
        with pytest.raises(ToolExecutionError, match="private address"):
            _check_url_safety("http://192.168.1.1/")

    def test_allows_private_when_flagged(self):
        result = _check_url_safety("http://127.0.0.1/", allow_private=True)
        assert result == "http://127.0.0.1/"

    def test_allows_https(self):
        result = _check_url_safety("https://docs.python.org/3/")
        assert result == "https://docs.python.org/3/"

    def test_blocks_unsupported_scheme(self):
        with pytest.raises(ToolExecutionError, match="blocked scheme"):
            _check_url_safety("ftp://files.example.com/data.csv")


class TestWebFetch:
    @patch("looplane.tooling.web.httpx.Client")
    def test_fetches_and_extracts(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.content = b"<html><body><p>Hello World</p></body></html>"
        mock_response.encoding = "utf-8"
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = web_fetch("https://example.com")
        assert "Fetched: https://example.com" in result
        assert "Hello World" in result

    @patch("looplane.tooling.web.httpx.Client")
    def test_truncates_long_content(self, mock_client_cls):
        long_text = "x" * 20_000
        mock_response = MagicMock()
        mock_response.content = f"<html><body><p>{long_text}</p></body></html>".encode()
        mock_response.encoding = "utf-8"
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = web_fetch("https://example.com", max_chars=5000)
        assert "[truncated]" in result

    def test_rejects_private_url(self):
        with pytest.raises(ToolExecutionError):
            web_fetch("http://127.0.0.1/secret")


class TestWebSearch:
    def test_rejects_empty_query(self):
        with pytest.raises(ToolExecutionError, match="non-empty"):
            web_search("")

    def test_formats_results(self):
        from looplane.tooling.web import WebSearchResult, _SearchProvider

        class FakeProvider(_SearchProvider):
            name = "fake"

            def is_available(self):
                return True

            def search(self, query, *, max_results=5):
                return [
                    WebSearchResult(
                        title="Python Docs",
                        url="https://docs.python.org",
                        snippet="Official docs",
                    ),
                ]

        with patch("looplane.tooling.web._DEFAULT_CHAIN", [FakeProvider()]):
            result = web_search("python documentation")
        assert "Python Docs" in result
        assert "https://docs.python.org" in result
        assert "Official docs" in result

    def test_no_results(self):
        from looplane.tooling.web import _SearchProvider

        class EmptyProvider(_SearchProvider):
            name = "empty"

            def is_available(self):
                return True

            def search(self, query, *, max_results=5):
                return []

        with patch("looplane.tooling.web._DEFAULT_CHAIN", [EmptyProvider()]):
            result = web_search("xyznonexistent123456")
        assert "No results" in result


class TestHttpRequest:
    def test_rejects_bad_method(self):
        with pytest.raises(ToolExecutionError, match="unsupported method"):
            http_request("TRACE", "https://example.com")

    def test_rejects_private_url(self):
        with pytest.raises(ToolExecutionError):
            http_request("GET", "http://192.168.1.1/admin")

    @patch("looplane.tooling.web.httpx.Client")
    def test_sends_request(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason_phrase = "OK"
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = '{"status": "ok"}'
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = http_request("GET", "https://api.example.com/health")
        assert "HTTP 200 OK" in result
        assert '{"status": "ok"}' in result
