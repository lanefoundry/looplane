"""User/project instruction discovery for native prompt assembly."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MAX_INSTRUCTION_BYTES = 64 * 1024
PROJECT_INSTRUCTION_FILENAMES = ("AGENTS.md", "LOOPLANE.md")
PROJECT_OVERRIDE_INSTRUCTION_FILENAMES = ("AGENTS.override.md", "LOOPLANE.override.md")
InstructionScope = Literal["user", "project", "project_override"]
InstructionSourceStatus = Literal["active", "suppressed"]


@dataclass(frozen=True)
class InstructionDocument:
    source: str
    content: str
    scope: InstructionScope = "project"


@dataclass(frozen=True)
class InstructionSourceDiagnostic:
    source: str
    scope: InstructionScope
    status: InstructionSourceStatus
    reason: str


@dataclass(frozen=True)
class InstructionResolution:
    documents: tuple[InstructionDocument, ...]
    diagnostics: tuple[InstructionSourceDiagnostic, ...]


def default_user_instructions_path() -> Path:
    configured = os.environ.get("LOOPLANE_USER_INSTRUCTIONS")
    if configured:
        return Path(configured)
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_root / "looplane" / "instructions.md"


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

    return resolve_instruction_documents(
        project_root=project_root,
        start_dir=start_dir,
        user_path=user_path,
        max_bytes=max_bytes,
    ).documents


def resolve_instruction_documents(
    *,
    project_root: Path,
    start_dir: Path | None = None,
    user_path: Path | None = None,
    max_bytes: int = MAX_INSTRUCTION_BYTES,
) -> InstructionResolution:
    """Load instruction documents plus source-priority diagnostics."""

    documents: list[InstructionDocument] = []
    diagnostics: list[InstructionSourceDiagnostic] = []
    user_file = user_path or default_user_instructions_path()
    user_text = _read_instruction_file(user_file, max_bytes=max_bytes)
    if user_text is not None:
        document = InstructionDocument(source=str(user_file), content=user_text, scope="user")
        documents.append(document)
        diagnostics.append(
            InstructionSourceDiagnostic(
                source=document.source,
                scope=document.scope,
                status="active",
                reason="user instructions have highest configured priority",
            )
        )

    root = project_root.resolve(strict=True)
    for directory in _project_instruction_dirs(root, start_dir):
        override_documents: list[InstructionDocument] = []
        for filename in PROJECT_OVERRIDE_INSTRUCTION_FILENAMES:
            path = directory / filename
            text = _read_instruction_file(path, max_bytes=max_bytes)
            if text is not None:
                override_documents.append(
                    InstructionDocument(
                        source=path.relative_to(root).as_posix(),
                        content=text,
                        scope="project_override",
                    )
                )
        if override_documents:
            suppressed = tuple(document for document in documents if document.scope != "user")
            documents = [
                document for document in documents if document.scope == "user"
            ] + override_documents
            for document in suppressed:
                diagnostics.append(
                    InstructionSourceDiagnostic(
                        source=document.source,
                        scope=document.scope,
                        status="suppressed",
                        reason=(
                            "replaced by deeper project override "
                            + ", ".join(document.source for document in override_documents)
                        ),
                    )
                )
            for document in override_documents:
                diagnostics.append(
                    InstructionSourceDiagnostic(
                        source=document.source,
                        scope=document.scope,
                        status="active",
                        reason="project override replaces earlier project instruction layers",
                    )
                )
            continue
        for filename in PROJECT_INSTRUCTION_FILENAMES:
            path = directory / filename
            text = _read_instruction_file(path, max_bytes=max_bytes)
            if text is not None:
                document = InstructionDocument(
                    source=path.relative_to(root).as_posix(),
                    content=text,
                )
                documents.append(document)
                diagnostics.append(
                    InstructionSourceDiagnostic(
                        source=document.source,
                        scope=document.scope,
                        status="active",
                        reason="project instructions apply in root-to-leaf order",
                    )
                )
    active_sources = {document.source for document in documents}
    diagnostics = [
        diagnostic
        for diagnostic in diagnostics
        if diagnostic.status == "suppressed" or diagnostic.source in active_sources
    ]
    return InstructionResolution(
        documents=tuple(documents),
        diagnostics=tuple(diagnostics),
    )


def instruction_documents_fingerprint(documents: Iterable[InstructionDocument]) -> str:
    payload = "\n\n".join(
        f"{document.scope}\0{document.source}\0{document.content}" for document in documents
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def render_instruction_context(documents: Iterable[InstructionDocument]) -> str:
    docs = tuple(documents)
    if not docs:
        return ""
    sections = ["Additional instructions from configured files:"]
    for document in docs:
        sections.append(f"\nSource: {document.source}\n{document.content}")
    return "\n".join(sections)


def render_instruction_diagnostics(
    diagnostics: Iterable[InstructionSourceDiagnostic],
) -> str:
    """Render source-priority diagnostics for logs, prompts, and artifacts."""

    items = tuple(diagnostics)
    if not items:
        return ""
    lines = ["[instruction-source-priority-v1]"]
    for item in items:
        lines.append(f"- {item.status} {item.scope} {item.source}: {item.reason}")
    return "\n".join(lines)
