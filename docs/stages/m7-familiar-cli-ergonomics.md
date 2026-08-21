# M7: Familiar CLI ergonomics

## Scope

Make the project-owned Python agent feel like a daily coding CLI without replacing its loop or
weakening its safety boundaries. The primary entry is now `pca [PROMPT]` in the current directory,
with familiar print, exec, resume, directory, and model-selection conventions. Existing automation
using `pca run`, `--task`, and `--repo` remains valid.

## Baseline and acceptance criteria

M6 had a capable local and Cloudflare runtime, but ordinary use still required a long option list.
The root Typer group accepted only `--task`; `pca run` required repository, task, model, and check;
and `-p` meant provider even though Claude Code and Pi use it for non-interactive print mode.

M7 requires:

- `pca`, `pca [PROMPT]`, `pca -p [PROMPT]`, `pca exec [PROMPT]`, and `pca resume`;
- current-directory default plus `-C/--cd/--repo` aliases;
- compact `-m provider/model` selection;
- non-secret saved provider defaults with CLI > environment > config precedence;
- known subcommands taking precedence over the default positional-prompt route;
- unchanged disposable-workspace, approval, exact-check, and session semantics;
- compatibility coverage for legacy `run`, `--task`, and `--repo`.

## References studied

| Reference | Boundary used |
| --- | --- |
| Claude Code 2.1.238 local `--help` | optional prompt, `-p/--print`, continue/resume, model, and cwd conventions |
| Codex CLI 0.147.0 local `--help` | `codex [PROMPT]`, `exec`, `resume`, and `-C/--cd` vocabulary |
| Pi 0.70.6 local `--help` | positional messages, `-p`, provider/model selection, and session continuation |
| OpenCode 1.14.48 local `--help` | default TUI/project route, `run [message...]`, resume, and provider/model form |
| Existing PCA M2/M3 contracts | approval, session, headless JSON, exact verification, and source-isolation invariants |

OMP was not installed locally and no executable/package alias could be verified, so this stage does
not attribute a command convention to it. Attempts to refresh public reference pages through the
required `stealth_fetch` transport returned `Transport closed`; the comparison evidence is the
installed CLIs named above.

## Ideas borrowed

- Prompt and current working directory are the default interaction surface.
- `-p` means non-interactive print, while provider selection uses a long option.
- `exec` names the explicit headless path and `resume` remains a first-class command.
- `-C/--cd` selects a repository without changing the process-wide source contract.
- `provider/model` is accepted when the prefix names a supported provider.

## Adjustments made for this project

PCA is a command group rather than a single Click command. A narrow `DefaultCommandGroup` inserts
a hidden `chat` command only when the first argument is not a known command or group-owned help /
completion option. This preserves `pca resume`, `pca auth`, and other subcommand dispatch while
making arbitrary positional text the initial agent prompt.

`pca config` stores only `provider`, `model`, and `api_url`. The strict 64 KiB JSON schema rejects
unknown fields, symlinks, NULs, URL credentials, query strings, and fragments. Writes are atomic and
mode `0600`. A saved model/API endpoint is applied only when the resolved provider matches the saved
provider, preventing a temporary provider switch from inheriting an unrelated endpoint.

Interactive `pca` still uses `TTYApprovalPolicy` and live event projection. `pca -p` and `pca exec`
are headless and never read approval input; repository checks therefore retain the explicit
`--unsafe-local-exec` acknowledgement. Native-provider custom endpoints still require
`--allow-custom-provider-endpoint` rather than becoming trusted merely because an URL is present.

## Ideas deliberately not adopted

- No full-screen TUI, slash-command system, or multi-turn prompt editor was added.
- No secrets, OAuth grants, or API keys are copied into the convenience config.
- No automatic application of a patch back to the source repository was added.
- No fuzzy command correction was added: arbitrary non-command text is intentionally a prompt.
- `run`, `--task`, and `--repo` were not removed; breaking automation was unnecessary.
- `resume [session] [new prompt]` remains deferred because changing a durable task on resume needs a
  separate protocol decision, not only parser sugar.

## Implementation

- `src/coding_agent/cli.py`: default-command routing, positional prompt, `-p`, `exec`/`run`, cwd
  aliases, provider/model shorthand, config precedence, and consistent backend/gateway options.
- `src/coding_agent/cli_config.py`: strict, private, atomic non-secret defaults.
- `tests/test_cli.py`: root/subcommand routing, modern and legacy syntax, config precedence,
  completion routing, and migration errors.
- `tests/test_cli_config.py`: schema, credential URL, symlink, round-trip, and file-mode coverage.
- `README.md`: daily commands, config boundary, compatibility aliases, and current M6 status.

## Verification evidence

Release gates:

```text
uv run pytest -o addopts='' -q
uv run ruff check .
git diff --check
uv run pca --help
uv run pca exec --help
uv run pca config --help
```

Final results:

```text
201 passed in 28.87s
All checks passed!  # Ruff
source distribution and wheel built successfully
git diff --check: clean
independent review: GO
```

The independent review first found unsafe cross-provider config merging, changed headless
capability semantics, incomplete shell completion, a misleading `-p` migration error, and leaked
internal help naming. All were fixed and regression-tested. The final narrow review reproduced both
subcommand and default-prompt option completion and returned GO.

A real local Ollama `qwen3:4b` smoke used the new surface:

```text
PCA_CONFIG=/tmp/pca-m7-verified.hqkGDW/config.json pca config \
  --provider ollama --model qwen3:4b
PCA_CONFIG=/tmp/pca-m7-verified.hqkGDW/config.json pca exec \
  -C /tmp/pca-m7-verified.hqkGDW/repo \
  "Fix src/tiny_python_bug/calculator.py so the existing test passes." \
  --allowed-path "src/**" --check "pytest -q" \
  --tool-calling --unsafe-local-exec
```

Run `60d1b14436a147768f35715827aae3df` completed with terminal reason `verified`. It reached the
real provider, read the workspace, used `replace_text` for the one-line subtraction-to-addition
patch, and passed `pytest -q` (exit 0, one test). The uploaded source stayed clean at
`8f1cbc59f77676e0d051c745393c0cf97ce5f3d4`; the retained workspace diff equals
`changes.patch`. Evidence hashes are:

- `events.jsonl`: `86b263a3e536b038bae8b015b9fbb24bb32150d886d7678e3e6d6ef53f1a0202`
- `result.json`: `ffad75a83f49a54129c162edd609a99503fdac7382bb85184ef94ddd4f7b7cf6`
- `changes.patch`: `aabb0491ca737152246996e0d9c1139acfc8fde2083b1c5aa3583f460124e35c`

An earlier preliminary smoke was manually cancelled after its ad hoc fixture accidentally omitted
the package `__init__.py`. It remains diagnostic evidence only and is not the completion claim.

## Known limitations

- The default route is a streaming line-oriented CLI, not a full-screen TUI.
- Headless repository-code verification still needs `--unsafe-local-exec` locally; untrusted runs
  belong in the Cloudflare Sandbox service.
- Provider/model defaults are user-wide rather than repository-specific profiles.
- A one-word typo of a subcommand can be interpreted as a prompt, matching prompt-first CLIs.
- This is one bounded tiny-fixture completion, not a general reliability benchmark; M3 retains the
  repeatable 5/5 model eval evidence.

## Artifact paths

- Independent review: `.research/m7-cli-release-review.md` (generated before closure)
- Verified local smoke:
  `/tmp/pca-m7-verified.hqkGDW/runs/60d1b14436a147768f35715827aae3df`
- Draft practice article: `quidproquo/src/content/posts/ai/2026-08-22-python-coding-agent-familiar-cli.md`

## Commit

- Implementation: `dc09eb1`.
- Documentation/progress closure: this commit.
