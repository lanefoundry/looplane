"""Run a repeatable real-provider coding eval through the public headless CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "evals/live/tiny-python-bug.json"


def run_command(argv: list[str], *, cwd: Path, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def require_command(argv: list[str], *, cwd: Path) -> str:
    result = run_command(argv, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(argv)}\n{result.stderr}")
    return result.stdout.strip()


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if ".git" in path.relative_to(root).parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def prepare_source(fixture: Path, destination: Path) -> tuple[str, str]:
    shutil.copytree(
        fixture,
        destination,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache"),
    )
    require_command(["git", "init", "-q"], cwd=destination)
    require_command(["git", "config", "user.email", "eval@example.com"], cwd=destination)
    require_command(["git", "config", "user.name", "looplane Eval"], cwd=destination)
    require_command(["git", "add", "."], cwd=destination)
    require_command(["git", "commit", "-qm", "fixture: live provider eval"], cwd=destination)
    return require_command(["git", "rev-parse", "HEAD"], cwd=destination), tree_digest(destination)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def successful_tool_names(path: Path) -> list[str]:
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event_type") == "tool.completed":
            data = event.get("data", {})
            if data.get("ok") is not True:
                continue
            name = data.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def build_agent_command(
    *,
    config: dict[str, Any],
    source: Path,
    run_root: Path,
    provider: str,
    model: str,
    base_url: str | None,
    experimental_subscription: bool,
) -> list[str]:
    """Build the public headless CLI invocation retained in each eval record."""

    command = [
        sys.executable,
        "-m",
        "looplane",
        "run",
        "--repo",
        str(source),
        "--provider",
        provider,
        "--model",
        model,
        "--task",
        str(config["task"]),
        "--check",
        str(config["check"]),
        "--run-root",
        str(run_root),
        "--max-steps",
        str(config["max_steps"]),
        "--wall-time",
        str(config["wall_time_seconds"]),
        "--tool-calling",
        "--unsafe-local-exec",
    ]
    for allowed_path in config["allowed_paths"]:
        command.extend(("--allowed-path", str(allowed_path)))
    if base_url:
        command.extend(("--base-url", base_url))
    if experimental_subscription:
        command.append("--experimental-subscription")
    return command


def evaluate_attempt(
    *,
    attempt: int,
    config: dict[str, Any],
    output_root: Path,
    provider: str,
    model: str,
    base_url: str | None,
    experimental_subscription: bool,
) -> dict[str, Any]:
    attempt_root = output_root / f"attempt-{attempt:02d}"
    source = attempt_root / "source"
    run_root = attempt_root / "runs"
    attempt_root.mkdir(parents=True)
    fixture = PROJECT_ROOT / str(config["fixture"])
    base_sha, source_digest = prepare_source(fixture, source)
    run_root.mkdir()

    command = build_agent_command(
        config=config,
        source=source,
        run_root=run_root,
        provider=provider,
        model=model,
        base_url=base_url,
        experimental_subscription=experimental_subscription,
    )

    started = time.monotonic()
    process = run_command(
        command,
        cwd=PROJECT_ROOT,
        timeout=float(config["wall_time_seconds"]) + 60,
    )
    duration = time.monotonic() - started
    (attempt_root / "stdout.json").write_text(process.stdout, encoding="utf-8")
    (attempt_root / "stderr.log").write_text(process.stderr, encoding="utf-8")

    checks: dict[str, bool] = {"process_exit_zero": process.returncode == 0}
    result: dict[str, Any] = {}
    parse_error: str | None = None
    try:
        result = json.loads(process.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        parse_error = str(exc)
    checks["result_json"] = parse_error is None and isinstance(result, dict)
    if checks["result_json"]:
        checks["verified_completion"] = (
            result.get("status") == "completed"
            and result.get("terminal_reason") == "verified"
            and all(item.get("ok") is True for item in result.get("verification", []))
        )
        checks["changed_files"] = result.get("changed_files") == config["expected_changed_files"]
        artifacts = result.get("artifacts", {})
        patch_path = Path(str(artifacts.get("patch", "")))
        events_path = Path(str(artifacts.get("events", "")))
        patch = patch_path.read_text(encoding="utf-8") if patch_path.is_file() else ""
        tools = successful_tool_names(events_path) if events_path.is_file() else []
        checks["patch_contract"] = all(
            fragment in patch for fragment in config["expected_patch_fragments"]
        )
        checks["required_tool"] = config["required_tool"] in tools
    else:
        checks.update(
            verified_completion=False,
            changed_files=False,
            patch_contract=False,
            required_tool=False,
        )
        tools = []

    checks["source_head_unchanged"] = (
        require_command(["git", "rev-parse", "HEAD"], cwd=source) == base_sha
    )
    checks["source_status_clean"] = (
        require_command(["git", "status", "--porcelain"], cwd=source) == ""
    )
    checks["source_bytes_unchanged"] = tree_digest(source) == source_digest
    passed = all(checks.values())
    return {
        "attempt": attempt,
        "passed": passed,
        "duration_seconds": duration,
        "process_returncode": process.returncode,
        "parse_error": parse_error,
        "checks": checks,
        "run_id": result.get("run_id") if isinstance(result, dict) else None,
        "usage": result.get("usage") if isinstance(result, dict) else None,
        "tools": tools,
        "attempt_root": str(attempt_root),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url")
    parser.add_argument(
        "--experimental-subscription",
        action="store_true",
        help="Explicitly enable the experimental ChatGPT/Codex subscription transport.",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--attempts", type=int)
    parser.add_argument("--required-successes", type=int)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    config = load_json(args.manifest.resolve())
    attempts = args.attempts or int(config["daily_ready_attempts"])
    required = args.required_successes or int(config["daily_ready_successes"])
    if attempts < 1 or required < 1 or required > attempts:
        parser.error("attempts and required-successes must define a positive attainable threshold")
    output_root = args.output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=False)

    records: list[dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        record = evaluate_attempt(
            attempt=attempt,
            config=config,
            output_root=output_root,
            provider=args.provider,
            model=args.model,
            base_url=args.base_url,
            experimental_subscription=args.experimental_subscription,
        )
        records.append(record)
        print(
            f"attempt {attempt}/{attempts}: {'PASS' if record['passed'] else 'FAIL'} "
            f"({record['duration_seconds']:.2f}s)",
            flush=True,
        )

    successes = sum(record["passed"] for record in records)
    summary = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "eval_id": config["eval_id"],
        "provider": args.provider,
        "model": args.model,
        "base_url": args.base_url,
        "experimental_subscription": args.experimental_subscription,
        "attempts": attempts,
        "required_successes": required,
        "successes": successes,
        "daily_ready": successes >= required,
        "records": records,
    }
    summary_path = output_root / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"summary: {summary_path}")
    print(f"daily_ready: {summary['daily_ready']} ({successes}/{attempts})")
    return 0 if summary["daily_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
