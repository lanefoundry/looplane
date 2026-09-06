"""File and image attachment handling for looplane's TUI and CLI."""

from __future__ import annotations

import base64
import mimetypes
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_IMAGE_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

_BINARY_EXTENSIONS: frozenset[str] = _IMAGE_EXTENSIONS | frozenset({".pdf", ".svg"})

_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        # documents
        ".md",
        ".txt",
        ".rst",
        ".log",
        ".csv",
        ".tsv",
        # data
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".ini",
        ".cfg",
        ".env",
        # code
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".mjs",
        ".cjs",
        ".go",
        ".rs",
        ".rb",
        ".java",
        ".kt",
        ".scala",
        ".c",
        ".cpp",
        ".cc",
        ".h",
        ".hpp",
        ".cs",
        ".swift",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".sql",
        ".graphql",
        ".gql",
        ".html",
        ".css",
        ".scss",
        ".less",
        ".sass",
        ".r",
        ".R",
        ".lua",
        ".pl",
        ".pm",
        ".ex",
        ".exs",
        ".zig",
        ".nim",
        ".v",
        ".d",
        ".hs",
        ".ml",
        ".mli",
        ".vue",
        ".svelte",
        ".astro",
        ".dockerfile",
        ".tf",
        ".hcl",
        ".proto",
        ".thrift",
        ".avsc",
        # config
        ".gitignore",
        ".editorconfig",
        ".prettierrc",
    }
)

_SUPPORTED_EXTENSIONS: frozenset[str] = _BINARY_EXTENSIONS | _TEXT_EXTENSIONS

_MAX_BINARY_BYTES = 20 * 1024 * 1024  # 20 MB
_MAX_TEXT_BYTES = 512 * 1024  # 512 KB
_MAX_ATTACHMENTS_PER_MESSAGE = 10
_MAX_SESSION_ATTACHMENTS = 5


@dataclass(frozen=True, slots=True)
class Attachment:
    """A resolved, validated file attachment ready for provider submission."""

    name: str
    media_type: str
    size_bytes: int
    data_base64: str | None = None
    content: str | None = None

    def to_provider_dict(self) -> dict[str, str]:
        d: dict[str, str] = {"name": self.name, "media_type": self.media_type}
        if self.data_base64 is not None:
            d["data_base64"] = self.data_base64
        elif self.content is not None:
            d["content"] = self.content
        return d


class AttachmentError(ValueError):
    """Raised when a file cannot be attached."""


def _is_text_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in _TEXT_EXTENSIONS:
        return True
    if suffix in _BINARY_EXTENSIONS:
        return False
    if path.name.startswith(".") and not suffix:
        return True
    mt, _ = mimetypes.guess_type(str(path))
    return mt is not None and mt.startswith("text/")


def _detect_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    explicit: dict[str, str] = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".pdf": "application/pdf",
        ".json": "application/json",
        ".yaml": "application/x-yaml",
        ".yml": "application/x-yaml",
        ".toml": "application/toml",
        ".xml": "application/xml",
        ".csv": "text/csv",
        ".tsv": "text/tab-separated-values",
        ".md": "text/markdown",
        ".html": "text/html",
        ".css": "text/css",
    }
    if suffix in explicit:
        return explicit[suffix]
    mt, _ = mimetypes.guess_type(str(path))
    if mt:
        return mt
    return "text/plain"


def _is_supported_extension(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in _SUPPORTED_EXTENSIONS:
        return True
    if not suffix and path.name.startswith("."):
        return True
    mt, _ = mimetypes.guess_type(str(path))
    return mt is not None and (mt.startswith("text/") or mt.startswith("image/"))


def resolve_and_read(path: str | Path, *, base: Path | None = None) -> Attachment:
    """Read a file and return a validated Attachment.

    Raises AttachmentError for unsupported types, missing files, or size violations.
    """
    p = Path(path).expanduser()
    if not p.is_absolute() and base is not None:
        p = base / p
    p = p.resolve()

    if not p.is_file():
        raise AttachmentError(f"File not found: {path}")

    if not _is_supported_extension(p):
        raise AttachmentError(f"Unsupported file type: {p.suffix or p.name}")

    size = p.stat().st_size
    is_text = _is_text_file(p)

    if is_text and size > _MAX_TEXT_BYTES:
        size_kb = size // 1024
        limit_kb = _MAX_TEXT_BYTES // 1024
        raise AttachmentError(f"Text file too large: {size_kb}KB (limit {limit_kb}KB)")

    if not is_text and size > _MAX_BINARY_BYTES:
        size_mb = size // (1024 * 1024)
        limit_mb = _MAX_BINARY_BYTES // (1024 * 1024)
        raise AttachmentError(f"File too large: {size_mb}MB (limit {limit_mb}MB)")

    media_type = _detect_media_type(p)

    if is_text:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise AttachmentError(f"Cannot read file: {exc}") from exc
        return Attachment(
            name=p.name,
            media_type=media_type,
            size_bytes=size,
            content=text,
        )

    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise AttachmentError(f"Cannot read file: {exc}") from exc
    return Attachment(
        name=p.name,
        media_type=media_type,
        size_bytes=size,
        data_base64=base64.b64encode(raw).decode("ascii"),
    )


def _strip_quotes(token: str) -> str:
    if len(token) >= 2 and (
        (token[0] == "'" and token[-1] == "'") or (token[0] == '"' and token[-1] == '"')
    ):
        return token[1:-1]
    return token


def detect_paths_in_text(text: str, *, base: Path) -> list[Path]:
    """Find file paths in prompt text that exist on disk and have supported extensions."""
    candidates: list[Path] = []
    for token in text.split():
        if token.startswith("\\"):
            continue
        cleaned = _strip_quotes(token.rstrip(",.;:!?"))
        if not cleaned:
            continue
        try:
            p = Path(cleaned).expanduser()
        except (ValueError, RuntimeError):
            continue
        if not p.is_absolute():
            p = base / p
        try:
            resolved = p.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.is_file() and _is_supported_extension(resolved):
            candidates.append(resolved)
    return candidates


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes // 1024}KB"
    return f"{size_bytes / (1024 * 1024):.1f}MB"


def format_attachment(attachment: Attachment) -> str:
    """Human-readable one-line summary for UI display."""
    return f"{attachment.name} ({_format_size(attachment.size_bytes)})"


def read_clipboard_image() -> Attachment | None:
    """Read an image from the system clipboard, or return None if unavailable."""
    raw = _read_clipboard_image_bytes()
    if raw is None:
        return None
    from time import strftime

    name = f"clipboard-{strftime('%H%M%S')}.png"
    return Attachment(
        name=name,
        media_type="image/png",
        size_bytes=len(raw),
        data_base64=base64.b64encode(raw).decode("ascii"),
    )


def _read_clipboard_image_bytes() -> bytes | None:
    if sys.platform == "darwin":
        return _read_clipboard_image_macos()
    return _read_clipboard_image_linux()


def _read_clipboard_image_macos() -> bytes | None:
    # Try pngpaste first (fast, reliable)
    if shutil.which("pngpaste"):
        try:
            result = subprocess.run(
                ["pngpaste", "-"],
                capture_output=True,
                timeout=3,
                check=False,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except (OSError, subprocess.SubprocessError):
            pass

    # Fallback: osascript to read PNGf class
    script = (
        'use framework "AppKit"\n'
        "set pb to current application's NSPasteboard's generalPasteboard()\n"
        'set imgData to pb\'s dataForType:"public.png"\n'
        'if imgData is missing value then return ""\n'
        "return (imgData's base64EncodedStringWithOptions:0) as text"
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return base64.b64decode(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _read_clipboard_image_linux() -> bytes | None:
    # Try xclip
    if shutil.which("xclip"):
        try:
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                capture_output=True,
                timeout=3,
                check=False,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except (OSError, subprocess.SubprocessError):
            pass

    # Try xsel (doesn't support binary types well, skip)
    # Try wl-paste for Wayland
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        try:
            result = subprocess.run(
                ["wl-paste", "--type", "image/png"],
                capture_output=True,
                timeout=3,
                check=False,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def validate_attachment_count(
    current: int,
    adding: int = 1,
    *,
    limit: int = _MAX_ATTACHMENTS_PER_MESSAGE,
) -> None:
    """Raise AttachmentError if adding would exceed the per-message limit."""
    if current + adding > limit:
        raise AttachmentError(f"Attachment limit reached ({limit})")


def validate_session_attachment_count(current: int, adding: int = 1) -> None:
    """Raise AttachmentError if adding would exceed the session context limit."""
    if current + adding > _MAX_SESSION_ATTACHMENTS:
        raise AttachmentError(
            f"Session context attachment limit reached ({_MAX_SESSION_ATTACHMENTS})"
        )
