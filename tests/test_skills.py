from __future__ import annotations

import pytest

from looplane.skills import (
    SkillError,
    load_project_skills,
    render_skill_context,
    render_skill_metadata,
    select_project_skills,
)


def test_load_project_skills_reads_bounded_markdown_frontmatter(tmp_path) -> None:
    skills = tmp_path / ".looplane" / "skills"
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
    assert loaded[0].source == ".looplane/skills/review.md"
    assert "Project skills from .looplane/skills" in rendered
    assert "Check regression risk" in rendered


def test_load_project_skills_rejects_symlink_and_bad_frontmatter(tmp_path) -> None:
    skills = tmp_path / ".looplane" / "skills"
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
    skills = tmp_path / ".looplane" / "skills"
    skills.mkdir(parents=True)
    (skills / "review.md").write_text("---\nname: reviewer\n---\nReview.", encoding="utf-8")
    (skills / "test.md").write_text("---\nname: test-writer\n---\nTest.", encoding="utf-8")
    loaded = load_project_skills(tmp_path)

    selected = select_project_skills(loaded, ("test-writer",))

    assert [skill.name for skill in selected] == ["test-writer"]
    with pytest.raises(SkillError, match="unknown enabled skill"):
        select_project_skills(loaded, ("missing",))


def test_render_skill_metadata_lists_names_and_descriptions(tmp_path) -> None:
    skills = tmp_path / ".looplane" / "skills"
    skills.mkdir(parents=True)
    (skills / "deploy.md").write_text(
        "---\nname: deploy\ndescription: Deploy to production.\n---\nDeploy steps.",
        encoding="utf-8",
    )
    (skills / "review.md").write_text(
        "---\nname: review\n---\nReview checklist.",
        encoding="utf-8",
    )
    loaded = load_project_skills(tmp_path)
    metadata = render_skill_metadata(loaded)

    assert "invoke_skill" in metadata
    assert "deploy - Deploy to production." in metadata
    assert "review" in metadata
    assert "Deploy steps." not in metadata
    assert "Review checklist." not in metadata


def test_render_skill_metadata_empty_returns_empty_string() -> None:
    assert render_skill_metadata(()) == ""


def test_execute_invoke_skill_finds_by_name(tmp_path) -> None:
    from looplane.agent.skill_dispatch import execute_invoke_skill
    from looplane.contracts import ToolCall

    skills = tmp_path / ".looplane" / "skills"
    skills.mkdir(parents=True)
    (skills / "deploy.md").write_text("---\nname: deploy\n---\nDeploy.", encoding="utf-8")
    (skills / "review.md").write_text("---\nname: review\n---\nReview.", encoding="utf-8")
    loaded = load_project_skills(tmp_path)

    call = ToolCall(tool_call_id="tc1", name="invoke_skill", arguments={"name": "deploy"})
    obs = execute_invoke_skill(call, loaded)
    assert obs.ok is True
    assert "Deploy." in obs.content

    call_miss = ToolCall(tool_call_id="tc2", name="invoke_skill", arguments={"name": "nonexistent"})
    obs_miss = execute_invoke_skill(call_miss, loaded)
    assert obs_miss.ok is False


def test_invoke_skill_definition_includes_skill_metadata(tmp_path) -> None:
    from looplane.agent.skill_dispatch import invoke_skill_definition

    skills = tmp_path / ".looplane" / "skills"
    skills.mkdir(parents=True)
    (skills / "deploy.md").write_text(
        "---\nname: deploy\ndescription: Ship it.\n---\nSteps.",
        encoding="utf-8",
    )
    loaded = load_project_skills(tmp_path)
    pairs = tuple((s.name, s.description) for s in loaded)
    definition = invoke_skill_definition(pairs)

    assert definition.name == "invoke_skill"
    assert definition.read_only is True
    assert "deploy - Ship it." in definition.description
    assert definition.input_schema["required"] == ["name"]


def test_execute_invoke_skill_returns_body(tmp_path) -> None:
    from looplane.agent.skill_dispatch import execute_invoke_skill
    from looplane.contracts import ToolCall

    skills = tmp_path / ".looplane" / "skills"
    skills.mkdir(parents=True)
    (skills / "deploy.md").write_text(
        "---\nname: deploy\ndescription: Ship it.\n---\nRun deploy script.",
        encoding="utf-8",
    )
    loaded = load_project_skills(tmp_path)
    call = ToolCall(tool_call_id="tc1", name="invoke_skill", arguments={"name": "deploy"})

    obs = execute_invoke_skill(call, loaded)

    assert obs.ok is True
    assert "Run deploy script." in obs.content
    assert "Ship it." in obs.content


def test_execute_invoke_skill_unknown_name(tmp_path) -> None:
    from looplane.agent.skill_dispatch import execute_invoke_skill
    from looplane.contracts import ToolCall

    skills = tmp_path / ".looplane" / "skills"
    skills.mkdir(parents=True)
    (skills / "deploy.md").write_text("---\nname: deploy\n---\nDeploy.", encoding="utf-8")
    loaded = load_project_skills(tmp_path)
    call = ToolCall(tool_call_id="tc1", name="invoke_skill", arguments={"name": "missing"})

    obs = execute_invoke_skill(call, loaded)

    assert obs.ok is False
    assert "Unknown skill" in (obs.error or "")
