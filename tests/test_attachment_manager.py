"""Tests for looplane.attachment_manager."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from looplane.attachment_manager import (
    _MAX_ATTACHMENTS_PER_MESSAGE,
    _MAX_SESSION_ATTACHMENTS,
    _MAX_TEXT_BYTES,
    Attachment,
    AttachmentError,
    detect_paths_in_text,
    format_attachment,
    resolve_and_read,
    validate_attachment_count,
    validate_session_attachment_count,
)

# ---------------------------------------------------------------------------
# resolve_and_read
# ---------------------------------------------------------------------------


class TestResolveAndRead:
    def test_png_image_returns_base64(self, tmp_path: Path) -> None:
        raw = b"\x89PNG\r\n\x1a\nfake-png-data"
        img = tmp_path / "screenshot.png"
        img.write_bytes(raw)

        att = resolve_and_read(img)

        assert att.name == "screenshot.png"
        assert att.media_type == "image/png"
        assert att.size_bytes == len(raw)
        assert att.data_base64 == base64.b64encode(raw).decode("ascii")
        assert att.content is None

    def test_jpeg_image(self, tmp_path: Path) -> None:
        raw = b"\xff\xd8\xff\xe0fake-jpeg"
        img = tmp_path / "photo.jpg"
        img.write_bytes(raw)

        att = resolve_and_read(img)

        assert att.media_type == "image/jpeg"
        assert att.data_base64 is not None

    def test_python_file_returns_content(self, tmp_path: Path) -> None:
        code = 'print("hello")\n'
        f = tmp_path / "script.py"
        f.write_text(code)

        att = resolve_and_read(f)

        assert att.name == "script.py"
        assert att.content == code
        assert att.data_base64 is None
        assert att.size_bytes == len(code.encode())

    def test_csv_file(self, tmp_path: Path) -> None:
        data = "a,b,c\n1,2,3\n"
        f = tmp_path / "data.csv"
        f.write_text(data)

        att = resolve_and_read(f)

        assert att.media_type == "text/csv"
        assert att.content == data

    def test_json_file(self, tmp_path: Path) -> None:
        data = '{"key": "value"}\n'
        f = tmp_path / "config.json"
        f.write_text(data)

        att = resolve_and_read(f)

        assert att.media_type == "application/json"
        assert att.content == data

    def test_nonexistent_file_raises(self) -> None:
        with pytest.raises(AttachmentError, match="File not found"):
            resolve_and_read("/no/such/file.png")

    def test_unsupported_type_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "archive.zip"
        f.write_bytes(b"PK\x03\x04fake-zip")

        with pytest.raises(AttachmentError, match="Unsupported file type"):
            resolve_and_read(f)

    def test_text_file_too_large_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "huge.py"
        f.write_bytes(b"x" * (_MAX_TEXT_BYTES + 1))

        with pytest.raises(AttachmentError, match="Text file too large"):
            resolve_and_read(f)

    def test_relative_path_with_base(self, tmp_path: Path) -> None:
        sub = tmp_path / "src"
        sub.mkdir()
        f = sub / "main.py"
        f.write_text("pass\n")

        att = resolve_and_read("src/main.py", base=tmp_path)

        assert att.name == "main.py"
        assert att.content == "pass\n"

    def test_svg_read_as_text(self, tmp_path: Path) -> None:
        svg_content = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>'
        f = tmp_path / "icon.svg"
        f.write_text(svg_content)

        att = resolve_and_read(f)

        # SVG is in _BINARY_EXTENSIONS so treated as binary
        assert att.media_type == "image/svg+xml"
        assert att.data_base64 is not None
        assert att.content is None

    def test_pdf_read_as_binary(self, tmp_path: Path) -> None:
        raw = b"%PDF-1.4 fake-pdf-content"
        f = tmp_path / "doc.pdf"
        f.write_bytes(raw)

        att = resolve_and_read(f)

        assert att.media_type == "application/pdf"
        assert att.data_base64 == base64.b64encode(raw).decode("ascii")
        assert att.content is None

    def test_markdown_file(self, tmp_path: Path) -> None:
        md = "# Title\n\nSome text.\n"
        f = tmp_path / "README.md"
        f.write_text(md)

        att = resolve_and_read(f)

        assert att.media_type == "text/markdown"
        assert att.content == md

    def test_webp_image(self, tmp_path: Path) -> None:
        raw = b"RIFF\x00\x00\x00\x00WEBPfake"
        f = tmp_path / "photo.webp"
        f.write_bytes(raw)

        att = resolve_and_read(f)

        assert att.media_type == "image/webp"
        assert att.data_base64 is not None


# ---------------------------------------------------------------------------
# detect_paths_in_text
# ---------------------------------------------------------------------------


class TestDetectPathsInText:
    def test_detects_valid_file_path(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("pass\n")

        result = detect_paths_in_text(f"look at {f}", base=tmp_path)

        assert len(result) == 1
        assert result[0] == f.resolve()

    def test_empty_prompt_returns_empty(self, tmp_path: Path) -> None:
        assert detect_paths_in_text("", base=tmp_path) == []

    def test_no_paths_returns_empty(self, tmp_path: Path) -> None:
        assert detect_paths_in_text("just some text", base=tmp_path) == []

    def test_escaped_path_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("pass\n")

        result = detect_paths_in_text(f"\\{f}", base=tmp_path)

        assert result == []

    def test_quoted_path_detected(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("a,b\n")

        result = detect_paths_in_text(f"look at '{f}'", base=tmp_path)

        assert len(result) == 1

    def test_double_quoted_path_detected(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("a,b\n")

        result = detect_paths_in_text(f'look at "{f}"', base=tmp_path)

        assert len(result) == 1

    def test_directory_not_detected(self, tmp_path: Path) -> None:
        d = tmp_path / "subdir"
        d.mkdir()

        result = detect_paths_in_text(f"check {d}", base=tmp_path)

        assert result == []

    def test_multiple_paths(self, tmp_path: Path) -> None:
        a = tmp_path / "a.py"
        b = tmp_path / "b.json"
        a.write_text("pass\n")
        b.write_text("{}\n")

        result = detect_paths_in_text(f"{a} and {b}", base=tmp_path)

        assert len(result) == 2

    def test_trailing_punctuation_stripped(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("pass\n")

        result = detect_paths_in_text(f"check {f},", base=tmp_path)
        assert len(result) == 1

        result = detect_paths_in_text(f"see {f}!", base=tmp_path)
        assert len(result) == 1

    def test_relative_path_resolved_against_base(self, tmp_path: Path) -> None:
        f = tmp_path / "hello.py"
        f.write_text("pass\n")

        result = detect_paths_in_text("hello.py", base=tmp_path)

        assert len(result) == 1
        assert result[0] == f.resolve()

    def test_unsupported_extension_not_detected(self, tmp_path: Path) -> None:
        f = tmp_path / "data.zip"
        f.write_bytes(b"PK\x03\x04")

        result = detect_paths_in_text(str(f), base=tmp_path)

        assert result == []


# ---------------------------------------------------------------------------
# format_attachment
# ---------------------------------------------------------------------------


class TestFormatAttachment:
    def test_bytes_size(self) -> None:
        att = Attachment(name="tiny.txt", media_type="text/plain", size_bytes=42)
        assert format_attachment(att) == "tiny.txt (42B)"

    def test_kb_size(self) -> None:
        att = Attachment(name="mid.py", media_type="text/plain", size_bytes=2048)
        assert format_attachment(att) == "mid.py (2KB)"

    def test_mb_size(self) -> None:
        att = Attachment(name="big.png", media_type="image/png", size_bytes=5 * 1024 * 1024)
        assert format_attachment(att) == "big.png (5.0MB)"


# ---------------------------------------------------------------------------
# validate_attachment_count
# ---------------------------------------------------------------------------


class TestValidateAttachmentCount:
    def test_under_limit_passes(self) -> None:
        validate_attachment_count(0)
        validate_attachment_count(9)

    def test_at_limit_raises(self) -> None:
        with pytest.raises(AttachmentError, match="Attachment limit reached"):
            validate_attachment_count(_MAX_ATTACHMENTS_PER_MESSAGE)

    def test_adding_multiple_at_limit_raises(self) -> None:
        with pytest.raises(AttachmentError):
            validate_attachment_count(8, adding=3)

    def test_custom_limit(self) -> None:
        validate_attachment_count(2, limit=3)
        with pytest.raises(AttachmentError):
            validate_attachment_count(3, limit=3)


class TestValidateSessionAttachmentCount:
    def test_under_limit_passes(self) -> None:
        validate_session_attachment_count(0)

    def test_at_limit_raises(self) -> None:
        with pytest.raises(AttachmentError, match="Session context attachment limit"):
            validate_session_attachment_count(_MAX_SESSION_ATTACHMENTS)


# ---------------------------------------------------------------------------
# Attachment.to_provider_dict
# ---------------------------------------------------------------------------


class TestAttachmentToProviderDict:
    def test_binary_attachment(self) -> None:
        att = Attachment(
            name="img.png",
            media_type="image/png",
            size_bytes=100,
            data_base64="AAAA",
        )
        d = att.to_provider_dict()

        assert d == {
            "name": "img.png",
            "media_type": "image/png",
            "data_base64": "AAAA",
        }
        assert "content" not in d

    def test_text_attachment(self) -> None:
        att = Attachment(
            name="code.py",
            media_type="text/plain",
            size_bytes=10,
            content="print(1)\n",
        )
        d = att.to_provider_dict()

        assert d == {
            "name": "code.py",
            "media_type": "text/plain",
            "content": "print(1)\n",
        }
        assert "data_base64" not in d

    def test_neither_payload_returns_name_and_type(self) -> None:
        att = Attachment(name="ref.txt", media_type="text/plain", size_bytes=0)
        d = att.to_provider_dict()

        assert d == {"name": "ref.txt", "media_type": "text/plain"}
