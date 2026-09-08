"""Agent definitions loaded from markdown files with YAML frontmatter.

Bundled agents ship under ``src/looplane/agents/*.md``.  User-global and
project-local directories can overlay or extend the bundled set.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)", re.DOTALL)

_BUNDLED_DIR = Path(__file__).resolve().parent.parent / "agents"

_KNOWN_FIELDS = frozenset(
    {
        "name",
        "description",
        "tools",
        "model",
        "max_steps",
        "allow_modify",
        "allow_execute",
        "isolation",
        "spawns",
    }
)


@dataclass(frozen=True)
class AgentDefinition:
    """A resolved agent type loaded from a markdown definition file."""

    name: str
    description: str
    system_prompt: str
    tools: list[str] | None = None
    model: str | None = None
    max_steps: int = 30
    allow_modify: bool = False
    allow_execute: bool = False
    isolation: str | None = None
    spawns: list[str] | str | None = None
    source: str = "bundled"
    file_path: str | None = None


@dataclass
class AgentRegistry:
    """Ordered collection of agent definitions, keyed by name."""

    _agents: dict[str, AgentDefinition] = field(default_factory=dict)

    def register(self, definition: AgentDefinition) -> None:
        self._agents[definition.name] = definition

    def get(self, name: str) -> AgentDefinition | None:
        return self._agents.get(name)

    def names(self) -> list[str]:
        return list(self._agents)

    def all(self) -> list[AgentDefinition]:
        return list(self._agents.values())

    def __len__(self) -> int:
        return len(self._agents)

    def __contains__(self, name: str) -> bool:
        return name in self._agents


def _parse_yaml_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Minimal YAML frontmatter parser — no PyYAML dependency.

    Handles the flat key-value structure used in agent definition files.
    Supports: strings, integers, booleans, null, and simple lists.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    raw_yaml, body = match.group(1), match.group(2)
    meta: dict[str, Any] = {}
    for line in raw_yaml.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, raw_value = line.partition(":")
        key = key.strip()
        raw_value = raw_value.strip()

        if (
            raw_value.startswith('"')
            and raw_value.endswith('"')
            or raw_value.startswith("'")
            and raw_value.endswith("'")
        ):
            meta[key] = raw_value[1:-1]
        elif raw_value == "null" or raw_value == "~" or raw_value == "":
            meta[key] = None
        elif raw_value == "true":
            meta[key] = True
        elif raw_value == "false":
            meta[key] = False
        elif raw_value.startswith("[") and raw_value.endswith("]"):
            inner = raw_value[1:-1].strip()
            if not inner:
                meta[key] = []
            else:
                items = []
                for item in inner.split(","):
                    item = item.strip().strip('"').strip("'")
                    if item:
                        items.append(item)
                meta[key] = items
        else:
            try:
                meta[key] = int(raw_value)
            except ValueError:
                meta[key] = raw_value

    return meta, body.strip()


def _load_definition_file(path: Path, source: str) -> AgentDefinition | None:
    """Parse one ``.md`` agent definition file into an ``AgentDefinition``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("cannot read agent definition: %s", path)
        return None

    meta, body = _parse_yaml_frontmatter(text)
    name = meta.get("name") or path.stem
    if not isinstance(name, str) or not name:
        logger.warning("agent definition missing name: %s", path)
        return None

    unknown_fields = set(meta) - _KNOWN_FIELDS
    if unknown_fields:
        logger.warning(
            "agent %s has unknown frontmatter fields: %s", name, ", ".join(sorted(unknown_fields))
        )

    description = meta.get("description", "")
    tools = meta.get("tools")
    if tools is not None and not isinstance(tools, list):
        tools = None
    model = meta.get("model")
    max_steps = meta.get("max_steps", 30)
    if not isinstance(max_steps, int) or max_steps < 1:
        max_steps = 30
    allow_modify = bool(meta.get("allow_modify", False))
    allow_execute = bool(meta.get("allow_execute", False))
    isolation = meta.get("isolation")
    if isolation not in (None, "worktree", "subdirectory"):
        isolation = None
    spawns = meta.get("spawns")

    return AgentDefinition(
        name=name,
        description=description or "",
        system_prompt=body,
        tools=tools,
        model=model,
        max_steps=max_steps,
        allow_modify=allow_modify,
        allow_execute=allow_execute,
        isolation=isolation,
        spawns=spawns,
        source=source,
        file_path=str(path),
    )


def _load_directory(directory: Path, source: str, registry: AgentRegistry) -> None:
    """Load all ``.md`` files from a directory into the registry."""
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.md")):
        definition = _load_definition_file(path, source)
        if definition is not None:
            registry.register(definition)


def load_agents(
    *,
    project_root: Path | None = None,
    user_dir: Path | None = None,
) -> AgentRegistry:
    """Load agent definitions from bundled, user-global, and project-local tiers.

    Later tiers override earlier ones (bundled < user < project).
    """
    registry = AgentRegistry()

    _load_directory(_BUNDLED_DIR, "bundled", registry)

    if user_dir is None:
        user_dir = Path.home() / ".looplane" / "agents"
    _load_directory(user_dir, "user", registry)

    if project_root is not None:
        project_dir = project_root / ".looplane" / "agents"
        _load_directory(project_dir, "project", registry)

    return registry
