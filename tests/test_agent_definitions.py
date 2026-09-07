"""Tests for agent_definitions: markdown-based agent definition loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from looplane.agent.agent_definitions import (
    AgentDefinition,
    AgentRegistry,
    _parse_yaml_frontmatter,
    load_agents,
)
from looplane.agent.subagent_dispatch import (
    FORK_CONTEXT_MARKER,
    MAX_SUBAGENT_DEPTH,
    build_forked_messages,
    can_spawn_at_depth,
    is_in_fork,
    resolve_agent_tools,
    resolve_agent_type,
    subagent_role_instruction,
    yield_result_definition,
)
from looplane.contracts import Message, ToolDefinition


class TestParseYamlFrontmatter:
    def test_basic_frontmatter(self):
        text = '---\nname: scout\ndescription: "Explore files"\n---\nBody here.'
        meta, body = _parse_yaml_frontmatter(text)
        assert meta["name"] == "scout"
        assert meta["description"] == "Explore files"
        assert body == "Body here."

    def test_no_frontmatter(self):
        text = "Just a body with no frontmatter."
        meta, body = _parse_yaml_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_boolean_values(self):
        text = "---\nallow_modify: true\nallow_execute: false\n---\n"
        meta, _ = _parse_yaml_frontmatter(text)
        assert meta["allow_modify"] is True
        assert meta["allow_execute"] is False

    def test_null_values(self):
        text = "---\nmodel: null\nisolation: ~\nspawns:\n---\n"
        meta, _ = _parse_yaml_frontmatter(text)
        assert meta["model"] is None
        assert meta["isolation"] is None
        assert meta["spawns"] is None

    def test_list_values(self):
        text = "---\ntools: [read_file, grep, glob]\n---\n"
        meta, _ = _parse_yaml_frontmatter(text)
        assert meta["tools"] == ["read_file", "grep", "glob"]

    def test_empty_list(self):
        text = "---\ntools: []\n---\n"
        meta, _ = _parse_yaml_frontmatter(text)
        assert meta["tools"] == []

    def test_integer_values(self):
        text = "---\nmax_steps: 12\n---\n"
        meta, _ = _parse_yaml_frontmatter(text)
        assert meta["max_steps"] == 12


class TestAgentRegistry:
    def test_register_and_get(self):
        registry = AgentRegistry()
        defn = AgentDefinition(name="test", description="d", system_prompt="p")
        registry.register(defn)
        assert registry.get("test") is defn
        assert "test" in registry
        assert len(registry) == 1

    def test_get_unknown(self):
        registry = AgentRegistry()
        assert registry.get("nonexistent") is None

    def test_override(self):
        registry = AgentRegistry()
        defn1 = AgentDefinition(name="a", description="d1", system_prompt="p1")
        defn2 = AgentDefinition(name="a", description="d2", system_prompt="p2")
        registry.register(defn1)
        registry.register(defn2)
        assert registry.get("a") is defn2
        assert len(registry) == 1


class TestLoadAgents:
    def test_bundled_agents_loaded(self):
        registry = load_agents()
        assert "scout" in registry
        assert "analyst" in registry
        assert "reviewer" in registry
        assert len(registry) >= 3

    def test_bundled_scout_definition(self):
        registry = load_agents()
        scout = registry.get("scout")
        assert scout is not None
        assert scout.source == "bundled"
        assert scout.allow_modify is False
        assert scout.allow_execute is False
        assert scout.max_steps == 6
        assert "scout" in scout.system_prompt.lower()

    def test_project_agents_override_bundled(self, tmp_path: Path):
        agent_dir = tmp_path / ".looplane" / "agents"
        agent_dir.mkdir(parents=True)
        (agent_dir / "scout.md").write_text(
            "---\nname: scout\ndescription: custom\nmax_steps: 20\n---\nCustom scout."
        )
        registry = load_agents(project_root=tmp_path)
        scout = registry.get("scout")
        assert scout is not None
        assert scout.source == "project"
        assert scout.max_steps == 20
        assert scout.system_prompt == "Custom scout."

    def test_project_adds_new_agent(self, tmp_path: Path):
        agent_dir = tmp_path / ".looplane" / "agents"
        agent_dir.mkdir(parents=True)
        (agent_dir / "coder.md").write_text(
            "---\nname: coder\ndescription: writes code\n"
            "allow_modify: true\nmax_steps: 20\n---\nYou are a coder."
        )
        registry = load_agents(project_root=tmp_path)
        assert "coder" in registry
        coder = registry.get("coder")
        assert coder.allow_modify is True
        assert coder.source == "project"


class TestResolveAgentType:
    def test_resolve_bundled(self):
        defn = resolve_agent_type("scout")
        assert defn.name == "scout"

    def test_resolve_unknown_raises(self):
        with pytest.raises(ValueError, match="unknown agent type"):
            resolve_agent_type("nonexistent_agent_xyz")


class TestSubagentRoleInstruction:
    def test_instruction_from_registry(self):
        instruction = subagent_role_instruction("scout")
        assert "scout" in instruction.lower()

    def test_instruction_with_enum(self):
        from looplane.agent.subagent_dispatch import SubagentRole

        instruction = subagent_role_instruction(SubagentRole.ANALYST)
        assert "analyst" in instruction.lower()


class TestBuildForkedMessages:
    def test_appends_directive_with_marker(self):
        parent = [
            Message(role="system", content="System prompt"),
            Message(role="user", content="Do something"),
        ]
        forked = build_forked_messages(parent, "Find test files")
        assert len(forked) == 3
        assert forked[-1].role == "user"
        assert FORK_CONTEXT_MARKER in forked[-1].content
        assert "Find test files" in forked[-1].content

    def test_preserves_parent_messages(self):
        parent = [
            Message(role="system", content="System"),
            Message(role="user", content="Task"),
            Message(role="assistant", content="Working..."),
        ]
        forked = build_forked_messages(parent, "Sub-task")
        assert forked[0] is parent[0]
        assert forked[1] is parent[1]
        assert forked[2] is parent[2]


class TestIsInFork:
    def test_detects_fork_marker(self):
        msgs = [
            Message(role="system", content="System"),
            Message(role="user", content=f"{FORK_CONTEXT_MARKER}\nDo X"),
        ]
        assert is_in_fork(msgs) is True

    def test_normal_messages_not_fork(self):
        msgs = [
            Message(role="system", content="System"),
            Message(role="user", content="Normal task"),
        ]
        assert is_in_fork(msgs) is False

    def test_roundtrip_with_build(self):
        parent = [Message(role="user", content="Hello")]
        forked = build_forked_messages(parent, "task")
        assert is_in_fork(forked) is True
        assert is_in_fork(parent) is False


class TestResolveAgentTools:
    @pytest.fixture()
    def parent_tools(self):
        return (
            ToolDefinition(name="read_file", description="Read", input_schema={}, read_only=True),
            ToolDefinition(name="edit_file", description="Edit", input_schema={}, read_only=False),
            ToolDefinition(name="grep", description="Grep", input_schema={}, read_only=True),
            ToolDefinition(name="run_shell", description="Shell", input_schema={}, read_only=False),
        )

    def test_none_returns_read_only(self, parent_tools):
        defn = AgentDefinition(name="s", description="", system_prompt="")
        result = resolve_agent_tools(defn, parent_tools)
        assert len(result) == 2
        assert all(t.read_only for t in result)

    def test_star_returns_all(self, parent_tools):
        defn = AgentDefinition(name="c", description="", system_prompt="", tools=["*"])
        result = resolve_agent_tools(defn, parent_tools)
        assert len(result) == 4

    def test_explicit_list(self, parent_tools):
        defn = AgentDefinition(
            name="x", description="", system_prompt="", tools=["read_file", "grep"]
        )
        result = resolve_agent_tools(defn, parent_tools)
        assert {t.name for t in result} == {"read_file", "grep"}


class TestCanSpawnAtDepth:
    def test_no_spawns_cannot_spawn(self):
        defn = AgentDefinition(name="s", description="", system_prompt="")
        assert can_spawn_at_depth(defn, 0) is False

    def test_spawns_star_can_spawn_within_depth(self):
        defn = AgentDefinition(name="o", description="", system_prompt="", spawns="*")
        assert can_spawn_at_depth(defn, 0) is True
        assert can_spawn_at_depth(defn, MAX_SUBAGENT_DEPTH - 1) is True
        assert can_spawn_at_depth(defn, MAX_SUBAGENT_DEPTH) is False

    def test_spawns_list_can_spawn(self):
        defn = AgentDefinition(name="o", description="", system_prompt="", spawns=["scout"])
        assert can_spawn_at_depth(defn, 0) is True

    def test_bundled_agents_cannot_spawn(self):
        for name in ("scout", "analyst", "reviewer", "coder"):
            defn = resolve_agent_type(name)
            assert can_spawn_at_depth(defn, 0) is False


class TestYieldResultDefinition:
    def test_tool_shape(self):
        defn = yield_result_definition()
        assert defn.name == "yield_result"
        assert defn.read_only is True
        assert "summary" in defn.input_schema["required"]
        assert "data" in defn.input_schema["properties"]
        assert "status" in defn.input_schema["properties"]
