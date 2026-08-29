"""User/project instruction discovery for native prompt assembly."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

MAX_INSTRUCTION_BYTES = 64 * 1024
PROJECT_INSTRUCTION_FILENAMES = ("AGENTS.md", "RIVUMI.md")


@dataclass(frozen=True)
class InstructionDocument:
    source: str
    content: str


def default_user_instructions_path() -> Path:
    configured = os.environ.get("RIVUMI_USER_INSTRUCTIONS")
    if configured:
        return Path(configured)
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_root / "rivumi" / "instructions.md"


def _read_instruction_file(path: Path, *, max_bytes: int) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"instruction file must be a regular file: {path}")
    payload = path.read_bytes()
    if len(payload) > max_bytes:
        raise ValueError(f"instruction file exceeds {max_bytes} bytes: {path}")
    try:
        text = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError(f"instruction file is not valid UTF-8: {path}") from exc
    return text or None


def _project_instruction_dirs(root: Path, start_dir: Path | None) -> tuple[Path, ...]:
    root = root.resolve(strict=True)
    if start_dir is None:
        start = root
    else:
        start = start_dir.resolve(strict=True)
        if not start.is_dir():
            start = start.parent
        if start != root and root not in start.parents:
            start = root
    relative = start.relative_to(root)
    dirs = [root]
    current = root
    for part in relative.parts:
        current = current / part
        dirs.append(current)
    return tuple(dirs)


def load_instruction_documents(
    *,
    project_root: Path,
    start_dir: Path | None = None,
    user_path: Path | None = None,
    max_bytes: int = MAX_INSTRUCTION_BYTES,
) -> tuple[InstructionDocument, ...]:
    """Load user then project instructions, with deeper project dirs later."""

    documents: list[InstructionDocument] = []
    user_file = user_path or default_user_instructions_path()
    user_text = _read_instruction_file(user_file, max_bytes=max_bytes)
    if user_text is not None:
        documents.append(InstructionDocument(source=str(user_file), content=user_text))

    root = project_root.resolve(strict=True)
    for directory in _project_instruction_dirs(root, start_dir):
        for filename in PROJECT_INSTRUCTION_FILENAMES:
            path = directory / filename
            text = _read_instruction_file(path, max_bytes=max_bytes)
            if text is not None:
                documents.append(
                    InstructionDocument(
                        source=path.relative_to(root).as_posix(),
                        content=text,
                    )
                )
    return tuple(documents)


def render_instruction_context(documents: Iterable[InstructionDocument]) -> str:
    docs = tuple(documents)
    if not docs:
        return ""
    sections = ["Additional instructions from configured files:"]
    for document in docs:
        sections.append(f"\nSource: {document.source}\n{document.content}")
    return "\n".join(sections)
