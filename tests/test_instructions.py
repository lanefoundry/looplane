from __future__ import annotations

from pathlib import Path

import pytest

from rivumi.instructions import load_instruction_documents, render_instruction_context


def test_load_instruction_documents_orders_user_root_and_subfolder(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    nested = project / "packages" / "app"
    nested.mkdir(parents=True)
    user_file = tmp_path / "user-instructions.md"
    user_file.write_text("Prefer terse output.", encoding="utf-8")
    (project / "AGENTS.md").write_text("Root guidance.", encoding="utf-8")
    (nested / "RIVUMI.md").write_text("Nested guidance.", encoding="utf-8")

    documents = load_instruction_documents(
        project_root=project,
        start_dir=nested,
        user_path=user_file,
    )

    assert [document.source for document in documents] == [
        str(user_file),
        "AGENTS.md",
        "packages/app/RIVUMI.md",
    ]
    rendered = render_instruction_context(documents)
    assert rendered.index("Prefer terse output.") < rendered.index("Root guidance.")
    assert rendered.index("Root guidance.") < rendered.index("Nested guidance.")


def test_load_instruction_documents_rejects_symlink(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    target = tmp_path / "target.md"
    target.write_text("hidden", encoding="utf-8")
    (project / "AGENTS.md").symlink_to(target)

    with pytest.raises(ValueError, match="regular file"):
        load_instruction_documents(project_root=project)
