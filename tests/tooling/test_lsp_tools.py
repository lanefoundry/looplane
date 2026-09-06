"""Tests for tooling.lsp_tools — LSP semantic query tools."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from looplane.tooling.lsp_tools import (
    _format_location,
    _uri_to_relpath,
    lsp_definition,
    lsp_references,
    lsp_symbols,
)
from looplane.tooling.types import ToolExecutionError


@pytest.fixture
def mock_server(tmp_path: Path):
    server = MagicMock()
    server.project_root = tmp_path
    type(server).running = PropertyMock(return_value=True)
    server.initialize = AsyncMock(return_value={})
    server.request = AsyncMock(return_value=[])
    server.open_document = AsyncMock()
    return server


class TestUriToRelpath:
    def test_converts_file_uri(self, tmp_path: Path):
        uri = (tmp_path / "src" / "main.py").as_uri()
        assert _uri_to_relpath(uri, tmp_path) == "src/main.py"

    def test_returns_full_path_for_outside(self):
        uri = "file:///other/project/main.py"
        result = _uri_to_relpath(uri, Path("/my/project"))
        assert result == "/other/project/main.py"


class TestFormatLocation:
    def test_formats_with_1_based_lines(self, tmp_path: Path):
        loc = {
            "uri": (tmp_path / "foo.py").as_uri(),
            "range": {"start": {"line": 9, "character": 4}},
        }
        result = _format_location(loc, tmp_path)
        assert result == "foo.py:10:5"


class TestLspSymbols:
    @pytest.mark.asyncio
    async def test_returns_no_symbols(self, mock_server):
        mock_server.request = AsyncMock(return_value=[])
        result = await lsp_symbols(mock_server, "Foo")
        assert "No symbols found" in result

    @pytest.mark.asyncio
    async def test_formats_workspace_symbols(self, mock_server):
        mock_server.request = AsyncMock(
            return_value=[
                {
                    "name": "MyClass",
                    "kind": 5,
                    "location": {
                        "uri": (mock_server.project_root / "src/models.py").as_uri(),
                        "range": {"start": {"line": 10, "character": 0}},
                    },
                }
            ]
        )
        result = await lsp_symbols(mock_server, "MyClass")
        assert "MyClass" in result
        assert "Class" in result
        assert "src/models.py:11:1" in result

    @pytest.mark.asyncio
    async def test_raises_when_not_running(self, mock_server):
        type(mock_server).running = PropertyMock(return_value=False)
        with pytest.raises(ToolExecutionError, match="not running"):
            await lsp_symbols(mock_server, "Foo")


class TestLspReferences:
    @pytest.mark.asyncio
    async def test_returns_no_references(self, mock_server, tmp_path: Path):
        (tmp_path / "main.py").write_text("x = 1\n")
        mock_server.request = AsyncMock(return_value=[])
        result = await lsp_references(mock_server, "main.py", 1, 1)
        assert "No references found" in result

    @pytest.mark.asyncio
    async def test_formats_references(self, mock_server, tmp_path: Path):
        (tmp_path / "main.py").write_text("x = 1\n")
        mock_server.request = AsyncMock(
            return_value=[
                {
                    "uri": (tmp_path / "main.py").as_uri(),
                    "range": {"start": {"line": 0, "character": 0}},
                },
                {
                    "uri": (tmp_path / "test.py").as_uri(),
                    "range": {"start": {"line": 5, "character": 3}},
                },
            ]
        )
        result = await lsp_references(mock_server, "main.py", 1, 1)
        assert "main.py:1:1" in result
        assert "test.py:6:4" in result

    @pytest.mark.asyncio
    async def test_raises_for_missing_file(self, mock_server):
        with pytest.raises(ToolExecutionError, match="file not found"):
            await lsp_references(mock_server, "nonexistent.py", 1, 1)


class TestLspDefinition:
    @pytest.mark.asyncio
    async def test_returns_no_definition(self, mock_server, tmp_path: Path):
        (tmp_path / "main.py").write_text("x = 1\n")
        mock_server.request = AsyncMock(return_value=[])
        result = await lsp_definition(mock_server, "main.py", 1, 1)
        assert "No definition found" in result

    @pytest.mark.asyncio
    async def test_formats_definition(self, mock_server, tmp_path: Path):
        (tmp_path / "main.py").write_text("x = 1\n")
        mock_server.request = AsyncMock(
            return_value={
                "uri": (tmp_path / "lib.py").as_uri(),
                "range": {"start": {"line": 42, "character": 0}},
            }
        )
        result = await lsp_definition(mock_server, "main.py", 1, 1)
        assert "lib.py:43:1" in result
