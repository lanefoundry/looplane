"""Session listing, replay, resume and telemetry export commands."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import typer

from looplane.commands import bootstrap as _bootstrap
from looplane.commands import common as _common
from looplane.commands import paths as _paths
from looplane.commands import session_index as _session_index
from looplane.commands import settings as _settings
from looplane.commands.ports import CommandServices


def _resolve_resume_dir(run_root: Path, session: str) -> Path:
    root = run_root.resolve(strict=True)
    if session != "last":
        candidate = (root / session).resolve(strict=True)
        if candidate.parent != root or candidate.name != session:
            raise typer.BadParameter("session must be 'last' or one safe run id")
        return candidate
    candidates: list[Path] = []
    for path in root.glob("*/session.json"):
        if not path.is_file() or path.parent.is_symlink():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("terminal") is False:
            candidates.append(path.parent)
    if not candidates:
        raise typer.BadParameter("no persisted sessions were found")
    return max(candidates, key=lambda path: (path / "session.json").stat().st_mtime_ns)


def resume(
    session: str = "last",
    run_root: Path = _paths.DEFAULT_RUN_ROOT,
    api_url: str | None = None,
    experimental_subscription: bool = False,
    allow_custom_provider_endpoint: bool = False,
    *,
    services: CommandServices,
) -> None:
    """Resume a validated non-terminal session in its existing disposable workspace."""

    from looplane.approvals import TTYApprovalPolicy
    from looplane.console import ConsoleEventSink, LiveEventProjection
    from looplane.session import SessionStore, SessionValidationError

    try:
        run_dir = _resolve_resume_dir(run_root, session)
        manifest = asyncio.run(SessionStore(run_dir).load())
        _, _, api_url = _settings._resolve_cli_settings(
            provider=manifest.provider_name,
            model=manifest.model_id,
            api_url=api_url,
            services=services,
        )
        selected_model = services.model_factory(
            provider=manifest.provider_name,
            model=manifest.model_id,
            base_url=api_url,
            tool_calling=True,
            allow_custom_provider_endpoint=allow_custom_provider_endpoint,
            experimental_subscription=experimental_subscription,
        )
        projection = LiveEventProjection(
            run_id=manifest.run_id,
            last_sequence=manifest.last_event_sequence,
        )
        result = asyncio.run(
            _bootstrap._resume_and_close(
                run_dir,
                selected_model,
                approval_policy=TTYApprovalPolicy(sys.stdin, sys.stderr),
                event_sink=ConsoleEventSink(sys.stderr, projection),
                services=services,
            )
        )
    except (OSError, SessionValidationError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _common._show_result(result)
    if result.status != "completed":
        raise typer.Exit(code=1)


def sessions(
    run_root: Path = _paths.DEFAULT_RUN_ROOT,
    limit: int = 20,
    show: str | None = None,
    replay: str | None = None,
    replay_json: str | None = None,
    fork_from_event: str | None = None,
    analyze_subagents: str | None = None,
    sequence: int | None = None,
    query: str | None = None,
) -> None:
    """List recent agent runs and saved conversations with their usage."""

    index = _session_index.SessionIndex(run_root=run_root, query=query)
    normalized_query = index.normalized_query

    detail_modes = tuple(
        name
        for name, value in (
            ("--show", show),
            ("--replay", replay),
            ("--replay-json", replay_json),
            ("--fork-from-event", fork_from_event),
            ("--analyze-subagents", analyze_subagents),
        )
        if value is not None
    )
    if len(detail_modes) > 1:
        raise typer.BadParameter(f"{' and '.join(detail_modes)} cannot be used together")
    if sequence is not None and fork_from_event is None:
        raise typer.BadParameter("--sequence requires --fork-from-event")

    if show is not None:
        run_dir = index.resolve_run_dir(show)
        if run_dir is None:
            typer.echo(f"error: no unique run matching {show!r} under {run_root}", err=True)
            raise typer.Exit(code=2)
        manifest = index.read_json(run_dir / "session.json")
        result = index.read_json(run_dir / "result.json")
        request = index.read_json(run_dir / "request.json")
        source = result or manifest or {}
        typer.echo(f"Run {run_dir.name}")
        typer.echo(f"status: {source.get('status') or source.get('phase') or '?'}")
        provider_name = source.get("provider_name") or source.get("provider") or "?"
        model_name = source.get("model_id") or source.get("model") or "?"
        typer.echo(f"model: {provider_name} / {model_name}")
        if request and request.get("instruction"):
            typer.echo(f"task: {request['instruction']}")
        if source.get("summary"):
            typer.echo(f"summary: {source['summary']}")
        events = index.read_events(run_dir / "events.jsonl")
        if not events:
            typer.echo("events: none")
            return
        typer.echo("events:")
        for event in events:
            sequence = event.get("sequence")
            event_type = event.get("event_type")
            detail = index.event_detail(event)
            prefix = f"{sequence:>4}" if isinstance(sequence, int) else "   ?"
            line = f"{prefix}  {event_type or '?'}"
            if detail:
                line = f"{line}  {detail}"
            typer.echo(line)
        return

    if replay is not None:
        from looplane.session_replay import ReplayValidationError, reduce_jsonl

        run_dir = index.resolve_run_dir(replay)
        if run_dir is None:
            typer.echo(f"error: no unique run matching {replay!r} under {run_root}", err=True)
            raise typer.Exit(code=2)
        try:
            replay_state = reduce_jsonl(run_dir / "events.jsonl")
        except ReplayValidationError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc

        typer.echo(f"Replay {run_dir.name}")
        typer.echo("state:")
        for key, value in replay_state.as_dict().items():
            if key == "timeline":
                continue
            typer.echo(f"  {key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
        typer.echo("timeline:")
        if not replay_state.timeline:
            typer.echo("  none")
            return
        for item in replay_state.timeline:
            parts = [f"{item.sequence:>4}", item.event_type]
            if item.turn_id is not None:
                parts.append(f"turn={item.turn_id}")
            if item.text is not None:
                parts.append(f"text={json.dumps(item.text, ensure_ascii=False)}")
            if item.detail is not None:
                parts.append(f"detail={json.dumps(item.detail, ensure_ascii=False)}")
            typer.echo("  " + "  ".join(parts))
        return

    if replay_json is not None:
        from looplane.session_replay import ReplayValidationError, reduce_jsonl

        run_dir = index.resolve_run_dir(replay_json)
        if run_dir is None:
            typer.echo(f"error: no unique run matching {replay_json!r} under {run_root}", err=True)
            raise typer.Exit(code=2)
        try:
            replay_state = reduce_jsonl(run_dir / "events.jsonl")
        except ReplayValidationError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(replay_state.canonical_json())
        return

    if fork_from_event is not None:
        from looplane.session_replay import ReplayValidationError, create_forked_run_from_event

        if sequence is None:
            raise typer.BadParameter("--fork-from-event requires --sequence")
        run_dir = index.resolve_run_dir(fork_from_event)
        if run_dir is None:
            typer.echo(
                f"error: no unique run matching {fork_from_event!r} under {run_root}",
                err=True,
            )
            raise typer.Exit(code=2)
        try:
            fork_seed = create_forked_run_from_event(
                source_run_dir=run_dir,
                run_root=run_root,
                sequence=sequence,
            )
        except ReplayValidationError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(fork_seed.canonical_json())
        return

    if analyze_subagents is not None:
        from looplane.subagents import analyze_subagent_schedule_jsonl

        run_dir = index.resolve_run_dir(analyze_subagents)
        if run_dir is None:
            typer.echo(
                f"error: no unique run matching {analyze_subagents!r} under {run_root}",
                err=True,
            )
            raise typer.Exit(code=2)
        try:
            analysis = analyze_subagent_schedule_jsonl(run_dir / "events.jsonl")
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(json.dumps(analysis.as_dict(), ensure_ascii=False, sort_keys=True))
        return

    rows: list[tuple[float, str, str, str, str, str]] = []
    if run_root.exists() and not run_root.is_symlink() and run_root.is_dir():
        run_dirs = sorted(
            (path for path in run_root.iterdir() if index.safe_session_dir(path)),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for run_dir in run_dirs:
            manifest = index.read_json(run_dir / "session.json")
            result = index.read_json(run_dir / "result.json")
            request = index.read_json(run_dir / "request.json")
            if manifest is None and result is None and request is None:
                continue
            source = manifest or result or request or {}
            status = str(source.get("status") or source.get("phase") or "?")
            model = str(source.get("model_id") or source.get("model") or "?")
            total = index.usage_total(source)
            wall = source.get("active_wall_time_seconds")
            wall_text = f"{wall:.0f}s" if isinstance(wall, (int, float)) else "-"
            search_parts = [
                run_dir.name,
                status,
                model,
                source.get("provider_name"),
                source.get("provider"),
                source.get("summary"),
                source.get("changed_files"),
                request.get("instruction") if request else None,
            ]
            search_parts.extend(index.event_search_parts(run_dir / "events.jsonl"))
            if not index.matches(search_parts):
                continue
            rows.append(
                (
                    run_dir.stat().st_mtime,
                    run_dir.name[:12],
                    status,
                    model,
                    f"{total:,}",
                    wall_text,
                )
            )
            if len(rows) >= limit:
                break

    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    conversation_root = state_root / "looplane" / "conversations"
    if conversation_root.exists() and not conversation_root.is_symlink():
        from looplane.conversation import ConversationStore

        try:
            conversations = asyncio.run(ConversationStore(conversation_root).list())
        except (OSError, ValueError):
            conversations = ()
        for conversation in conversations:
            model = conversation.model_override or "-"
            search_parts = [
                conversation.conversation_id,
                "conversation",
                conversation.runtime,
                conversation.model_override,
                conversation.title,
            ]
            if normalized_query is not None:
                try:
                    snapshot = asyncio.run(
                        ConversationStore(conversation_root).load(conversation.conversation_id)
                    )
                except (OSError, ValueError):
                    snapshot = None
                if snapshot is not None:
                    search_parts.extend(index.conversation_event_search_parts(snapshot.events))
            if not index.matches(search_parts):
                continue
            rows.append(
                (
                    conversation.updated_at.timestamp(),
                    conversation.conversation_id[:12],
                    "conversation",
                    model,
                    "-",
                    "-",
                )
            )
            if len(rows) >= limit:
                break

    rows.sort(key=lambda row: row[0], reverse=True)
    visible = rows[:limit]
    if not visible:
        if normalized_query:
            typer.echo(f"No sessions matching {query!r} under {run_root}")
        else:
            typer.echo(f"No sessions found under {run_root}")
        return
    typer.echo(f"{'ID':<14}{'STATUS':<16}{'MODEL':<24}{'TOKENS':>12}  TIME")
    for _mtime, session_id, status, model, tokens, wall_text in visible:
        typer.echo(f"{session_id:<14}{status:<16}{model:<24}{tokens:>12}  {wall_text}")


def export_otel(
    run_id: str, run_root: Path = _paths.DEFAULT_RUN_ROOT, output: Path | None = None
) -> None:
    """Export a run as OpenTelemetry GenAI OTLP-JSON."""

    from looplane.otel_export import export_run

    run_dir = run_root / run_id
    if run_id == "last" or not run_dir.exists():
        candidates = sorted(
            (
                path
                for path in run_root.glob("*/result.json")
                if path.parent.name.startswith(run_id)
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            typer.echo(f"error: no run matching '{run_id}' under {run_root}", err=True)
            raise typer.Exit(code=2)
        run_dir = candidates[0].parent
    try:
        payload = export_run(run_dir, output)
    except FileNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if output is None:
        typer.echo(payload)
