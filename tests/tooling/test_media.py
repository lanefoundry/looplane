"""Tests for tooling.media — view_image and take_screenshot."""

from __future__ import annotations

from pathlib import Path

import pytest

from looplane.tooling.media import view_image
from looplane.tooling.types import ToolExecutionError


class TestViewImage:
    def test_rejects_missing_file(self, tmp_path: Path):
        with pytest.raises(ToolExecutionError, match="file not found"):
            view_image("nonexistent.png", workspace=tmp_path)

    def test_rejects_unsupported_format(self, tmp_path: Path):
        (tmp_path / "doc.pdf").write_bytes(b"fake pdf")
        with pytest.raises(ToolExecutionError, match="unsupported image format"):
            view_image("doc.pdf", workspace=tmp_path)

    def test_svg_returns_text(self, tmp_path: Path):
        svg_content = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="10"/></svg>'
        (tmp_path / "icon.svg").write_text(svg_content)
        result = view_image("icon.svg", workspace=tmp_path)
        assert isinstance(result, str)
        assert "circle" in result
        assert "SVG: icon.svg" in result

    def test_metadata_fallback_without_vision(self, tmp_path: Path):
        try:
            from PIL import Image

            img = Image.new("RGB", (100, 50), color="red")
            img.save(tmp_path / "red.png")
        except ImportError:
            pytest.skip("Pillow not installed")

        result = view_image("red.png", workspace=tmp_path, supports_vision=False)
        assert isinstance(result, str)
        assert "100x50" in result
        assert "metadata only" in result

    def test_returns_multimodal_with_vision(self, tmp_path: Path):
        try:
            from PIL import Image

            img = Image.new("RGB", (100, 50), color="blue")
            img.save(tmp_path / "blue.png")
        except ImportError:
            pytest.skip("Pillow not installed")

        result = view_image("blue.png", workspace=tmp_path, supports_vision=True)
        assert isinstance(result, dict)
        assert result["__multimodal__"] is True
        assert result["type"] == "image"
        assert "data" in result["source"]

    def test_resizes_large_image(self, tmp_path: Path):
        try:
            from PIL import Image

            img = Image.new("RGB", (4000, 3000), color="green")
            img.save(tmp_path / "big.png")
        except ImportError:
            pytest.skip("Pillow not installed")

        result = view_image("big.png", workspace=tmp_path, max_dimension=512)
        assert isinstance(result, dict)
        assert result["metadata"]["original_size"] == "4000x3000"

    def test_rejects_oversized_file(self, tmp_path: Path):
        huge = tmp_path / "huge.png"
        huge.write_bytes(b"\x89PNG" + b"\x00" * (11 * 1024 * 1024))
        with pytest.raises(ToolExecutionError, match="too large"):
            view_image("huge.png", workspace=tmp_path)
