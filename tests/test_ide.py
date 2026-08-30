from __future__ import annotations

import json

import pytest

from rivumi.ide import (
    EditorDeepLinkStyle,
    IdeBridgeError,
    IdeDiagnosticSeverity,
    IdePosition,
    build_editor_deep_link,
    load_project_ide_diagnostics,
    load_project_open_files,
    render_ide_diagnostics_context,
    render_ide_open_files_context,
)


def test_project_ide_diagnostics_loads_lsp_publish_diagnostics(tmp_path) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("x = 1\n", encoding="utf-8")
    diagnostics = tmp_path / ".rivumi" / "ide"
    diagnostics.mkdir(parents=True)
    (diagnostics / "diagnostics.json").write_text(
        json.dumps(
            {
                "uri": source.as_uri(),
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                        "severity": 2,
                        "source": "pyright",
                        "code": "reportUnusedExpression",
                        "message": "Unused expression",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    snapshot = load_project_ide_diagnostics(tmp_path)

    assert snapshot is not None
    assert snapshot.diagnostics[0].path == "src/app.py"
    assert snapshot.diagnostics[0].severity is IdeDiagnosticSeverity.WARNING
    context = render_ide_diagnostics_context(snapshot)
    assert context.startswith("[ide-lsp-diagnostics-v1]")
    assert "src/app.py:1:1: warning [pyright] reportUnusedExpression" in context
    linked_context = render_ide_diagnostics_context(snapshot, project_root=tmp_path)
    assert "deep_link=vscode://file/" in linked_context
    assert linked_context.endswith(":1:1)")


def test_project_ide_diagnostics_rejects_outside_file_uri(tmp_path) -> None:
    diagnostics = tmp_path / ".rivumi" / "ide"
    diagnostics.mkdir(parents=True)
    (diagnostics / "diagnostics.json").write_text(
        json.dumps(
            {
                "uri": (tmp_path.parent / "outside.py").as_uri(),
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                        "message": "outside",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(IdeBridgeError, match="inside repository"):
        load_project_ide_diagnostics(tmp_path)


def test_project_open_files_loads_editor_state(tmp_path) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("x = 1\n", encoding="utf-8")
    open_files = tmp_path / ".rivumi" / "ide"
    open_files.mkdir(parents=True)
    (open_files / "open-files.json").write_text(
        json.dumps(
            {
                "files": [
                    {
                        "uri": source.as_uri(),
                        "active": True,
                        "cursor": {"line": 0, "character": 4},
                        "selection": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 5},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    snapshot = load_project_open_files(tmp_path)

    assert snapshot is not None
    assert snapshot.files[0].path == "src/app.py"
    context = render_ide_open_files_context(snapshot)
    assert context.startswith("[ide-open-files-v1]")
    assert "src/app.py (active, cursor=1:5, selection=1:1-1:6)" in context
    linked_context = render_ide_open_files_context(snapshot, project_root=tmp_path)
    assert "src/app.py (active, cursor=1:5, selection=1:1-1:6)" in linked_context
    assert "deep_link=vscode://file/" in linked_context
    assert linked_context.endswith(":1:5]")


def test_editor_deep_link_uses_repo_relative_path_and_one_based_position(tmp_path) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("x = 1\n", encoding="utf-8")

    link = build_editor_deep_link(
        "src/app.py",
        project_root=tmp_path,
        position=IdePosition(line=0, character=4),
    )

    assert link.startswith("vscode://file/")
    assert link.endswith("/src/app.py:1:5")


def test_editor_deep_link_supports_file_uri_without_location(tmp_path) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("x = 1\n", encoding="utf-8")

    link = build_editor_deep_link(
        "src/app.py",
        project_root=tmp_path,
        editor=EditorDeepLinkStyle.FILE,
    )

    assert link == source.as_uri()


def test_editor_deep_link_rejects_paths_outside_repository(tmp_path) -> None:
    with pytest.raises(ValueError, match="inside repository"):
        build_editor_deep_link(
            str(tmp_path.parent / "outside.py"),
            project_root=tmp_path,
        )
