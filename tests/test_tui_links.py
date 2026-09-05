from pathlib import Path

from looplane.tui_links import resolve_transcript_link


def test_resolve_transcript_link_allows_complete_http_urls(tmp_path: Path) -> None:
    resolved = resolve_transcript_link(tmp_path, "https://example.com/docs?q=looplane")

    assert resolved is not None
    assert resolved.url == "https://example.com/docs?q=looplane"
    assert resolved.repository_path is None


def test_resolve_transcript_link_allows_repository_files_with_line_suffix(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("print('ok')\n")

    resolved = resolve_transcript_link(tmp_path, "src/example.py:12:3")

    assert resolved is not None
    assert resolved.url == source.as_uri()
    assert resolved.repository_path == source


def test_resolve_transcript_link_allows_local_file_urls_inside_repository(
    tmp_path: Path,
) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# Example\n")

    resolved = resolve_transcript_link(tmp_path, f"{readme.as_uri()}:7")

    assert resolved is not None
    assert resolved.url == readme.as_uri()


def test_resolve_transcript_link_rejects_unsafe_or_external_targets(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-looplane-link.txt"
    outside.write_text("outside\n")
    try:
        rejected = (
            "javascript:alert(1)",
            "data:text/plain,hello",
            "https://user:password@example.com/private",
            "https://example.com/line\nbreak",
            "file://server/share/example.py",
            str(outside),
            "missing.py:12",
        )

        assert all(resolve_transcript_link(tmp_path, href) is None for href in rejected)
    finally:
        outside.unlink()
