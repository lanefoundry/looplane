"""Image viewing and web screenshot tools."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

from looplane.tooling.types import ToolExecutionError

_MAX_DIMENSION = 2048
_DEFAULT_DIMENSION = 1024
_SUPPORTED_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"})
_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB


@dataclass
class ImageMetadata:
    path: str
    format: str
    width: int
    height: int
    size_bytes: int


def view_image(
    path: str,
    *,
    workspace: Path,
    max_dimension: int = _DEFAULT_DIMENSION,
    supports_vision: bool = True,
) -> str | dict:
    resolved = (workspace / path).resolve()
    if not resolved.is_file():
        raise ToolExecutionError(f"file not found: {path}")

    suffix = resolved.suffix.lower()
    if suffix not in _SUPPORTED_IMAGE_EXTENSIONS:
        raise ToolExecutionError(
            f"unsupported image format: {suffix}. "
            f"Supported: {', '.join(sorted(_SUPPORTED_IMAGE_EXTENSIONS))}"
        )

    file_size = resolved.stat().st_size
    if file_size > _MAX_IMAGE_BYTES:
        raise ToolExecutionError(f"image too large: {file_size} bytes (max {_MAX_IMAGE_BYTES})")

    if suffix == ".svg":
        text = resolved.read_text(encoding="utf-8", errors="replace")
        return f"# SVG: {path}\n\n```svg\n{text[:50_000]}\n```"

    max_dimension = min(max(max_dimension, 64), _MAX_DIMENSION)

    try:
        from PIL import Image
    except ImportError as exc:
        raise ToolExecutionError(
            "view_image requires the 'Pillow' package. Install with: pip install Pillow"
        ) from exc

    with Image.open(resolved) as img:
        original_width, original_height = img.size
        img_format = img.format or suffix.lstrip(".").upper()

        metadata = ImageMetadata(
            path=path,
            format=img_format,
            width=original_width,
            height=original_height,
            size_bytes=file_size,
        )

        if not supports_vision:
            return (
                f"# Image: {path}\n\n"
                f"Format: {metadata.format}\n"
                f"Size: {metadata.width}x{metadata.height}\n"
                f"File size: {metadata.size_bytes:,} bytes\n\n"
                "(Vision not supported by current model — metadata only)"
            )

        if original_width > max_dimension or original_height > max_dimension:
            ratio = min(max_dimension / original_width, max_dimension / original_height)
            new_size = (int(original_width * ratio), int(original_height * ratio))
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        import io

        buf = io.BytesIO()
        save_format = "PNG" if img_format in ("PNG", "BMP") else "JPEG"
        img.save(buf, format=save_format)
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")

    mime = "image/png" if save_format == "PNG" else "image/jpeg"
    return {
        "__multimodal__": True,
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": encoded,
        },
        "metadata": {
            "path": path,
            "original_size": f"{original_width}x{original_height}",
            "format": img_format,
        },
    }


def take_screenshot(
    url: str,
    *,
    selector: str | None = None,
    width: int = 1280,
    height: int = 720,
) -> str | dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ToolExecutionError(
            "take_screenshot requires 'playwright'. "
            "Install with: pip install playwright && playwright install chromium"
        ) from exc

    width = min(max(width, 320), 3840)
    height = min(max(height, 240), 2160)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(url, timeout=15_000)
        page.wait_for_load_state("networkidle", timeout=10_000)

        if selector:
            element = page.query_selector(selector)
            screenshot_bytes = element.screenshot() if element else page.screenshot(full_page=False)
        else:
            screenshot_bytes = page.screenshot(full_page=False)

        browser.close()

    encoded = base64.b64encode(screenshot_bytes).decode("ascii")
    return {
        "__multimodal__": True,
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": encoded,
        },
        "metadata": {
            "url": url,
            "viewport": f"{width}x{height}",
            "selector": selector,
        },
    }
