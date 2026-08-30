from __future__ import annotations

import pytest

from rivumi.skills import (
    SkillError,
    load_project_skills,
    render_skill_context,
    select_project_skills,
)


def test_load_project_skills_reads_bounded_markdown_frontmatter(tmp_path) -> None:
    skills = tmp_path / ".rivumi" / "skills"
    skills.mkdir(parents=True)
    (skills / "review.md").write_text(
        """---
name: reviewer
description: Review patch risks.
---
# Review

Check regression risk before final output.
""",
        encoding="utf-8",
    )

    loaded = load_project_skills(tmp_path)
    rendered = render_skill_context(loaded)

    assert len(loaded) == 1
    assert loaded[0].name == "reviewer"
    assert loaded[0].description == "Review patch risks."
    assert loaded[0].source == ".rivumi/skills/review.md"
    assert "Project skills from .rivumi/skills" in rendered
    assert "Check regression risk" in rendered


def test_load_project_skills_rejects_symlink_and_bad_frontmatter(tmp_path) -> None:
    skills = tmp_path / ".rivumi" / "skills"
    skills.mkdir(parents=True)
    target = tmp_path / "outside.md"
    target.write_text("outside", encoding="utf-8")
    (skills / "linked.md").symlink_to(target)

    with pytest.raises(SkillError, match="regular markdown file"):
        load_project_skills(tmp_path)

    (skills / "linked.md").unlink()
    (skills / "bad.md").write_text("---\nunsupported: value\n---\nbody", encoding="utf-8")

    with pytest.raises(SkillError, match="unsupported"):
        load_project_skills(tmp_path)


def test_select_project_skills_uses_exact_on_demand_names(tmp_path) -> None:
    skills = tmp_path / ".rivumi" / "skills"
    skills.mkdir(parents=True)
    (skills / "review.md").write_text("---\nname: reviewer\n---\nReview.", encoding="utf-8")
    (skills / "test.md").write_text("---\nname: test-writer\n---\nTest.", encoding="utf-8")
    loaded = load_project_skills(tmp_path)

    selected = select_project_skills(loaded, ("test-writer",))

    assert [skill.name for skill in selected] == ["test-writer"]
    with pytest.raises(SkillError, match="unknown enabled skill"):
        select_project_skills(loaded, ("missing",))
