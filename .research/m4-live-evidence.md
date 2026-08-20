# M4 live provider and CLI evidence

Date: 2026-08-21 (Asia/Taipei)

## What is proven

- Remote OpenAI-compatible endpoint: Groq `openai/gpt-oss-120b`, five independent full coding
  attempts, 5/5 completed and verified. Every attempt used `list_files`, `read_file`,
  `replace_text`, and `run_check`; all produced the same reviewable patch and left the source Git
  repository unchanged.
- Credential scan: the exact Groq API-key bytes had zero matches across all retained M4 remote,
  TTY, and resume evidence files.
- Bare TTY: global `pca` completed run `a66038f97f204cb3aaa96339f195c37e` after real execute,
  modify, and session-scoped execute approvals. It ended `completed` / `verified`.
- Resume: run `2fa14fe342ff4f81a6ad2dc22cd8ffda` was killed while the current manifest was
  `waiting_approval` at event sequence 5. `pca resume` emitted `session.resumed` at sequence 6,
  reconciled the pending request, obtained new approvals, edited, checked, and ended verified at
  sequence 54.
- Claude Code backend: the public command returned `CLAUDE_BACKEND_OK` through the installed
  official CLI and normalized stream-JSON events. It ran with no tools, ephemeral cwd, no session
  persistence, bounded I/O/time, and no forwarded API/OAuth/token environment variables. The
  official child retained `HOME` solely to resolve the authentication state it owns.

## What is not proven

- Claude delegated success is a subscription-connectivity and boundary test, not PCA coding-loop
  E2E and not repository editing.
- PCA's app-owned Codex OAuth grant still requires a successful user browser authorization. The
  official Codex CLI login is not imported and does not count as PCA provider evidence.
- The Codex OAuth client identity remains experimental pending project-owned registration and
  current authorization evidence.

## Remote repetition evidence

Retained at `.research/evidence/m4/remote-groq/`. The summary SHA-256 is
`253891f6e823872640997ee6788a4648de6f2f1fd3297f0660101017b113d4a3`.

Durations were 11.86, 40.86, 41.49, 42.01, and 46.11 seconds. The common patch SHA-256 is
`aabb0491ca737152246996e0d9c1139acfc8fde2083b1c5aa3583f460124e35c`.

| Run id | `events.jsonl` SHA-256 | `result.json` SHA-256 |
| --- | --- | --- |
| `1c412bfe918b41828f94fe872ae90273` | `20cc445f25ad196df87749fce5bff2993f89ca3b6a623fd973ef09f9d849d244` | `aeb0f1dea6f32de103be186c7a609a35eb69ec98d99f6675e3ddb10ef3ea2918` |
| `d658c13d71cc4411bf1cd6bbb3725125` | `b505aab7261be4bedc16df35beb30e427d1bc9b2ca9603f4e396ec342322c505` | `c742167b9bc414f40a8604bbbf51cab95d5fee1fd639b80a2cb8485de4f20085` |
| `ee729280a37143459f18855022017018` | `c94b2076eac5e5790c21b007b28c060d6b04058d242c6c371af2e6e90da5ef02` | `5d5bedf3db7a36f52c2b73d132a63cc244bb3f4b6ca7fe3cdc55fc8859d5c9d4` |
| `d10155c3fbb1479cbd1a27a0fdc10455` | `a30e7f4a2f58008dcf3b740782ebd16b352aba548186c6f66f2521cdfde0f7ef` | `0b0319b30ca59f1ce28cb9181f619b1523a5466fde6b4426b24c994f0b42c0e4` |
| `603c6587ac5a4868bb1606ee6b7abb2d` | `418669e05616b87fffcb4614667669c9b6e82d969463154be359a04e01fef153` | `4ba3e5f2670c7d37b03f46086490cc7f2fbbb145a9b3f0385ba50df55577a20a` |

## Release gates at this snapshot

```text
uv run pytest
165 passed in 19.02s

uv run ruff check .
All checks passed!

uv build
Successfully built source distribution and wheel

git diff --check
passed
```

## Re-run commands

```bash
export OPENAI_API_KEY='<authorized key>'
uv run python scripts/eval_live_provider.py \
  --provider openai-compatible \
  --model openai/gpt-oss-120b \
  --base-url https://api.groq.com/openai/v1 \
  --output-dir /absolute/new/output/path

pca backend claude-code --experimental-subscription \
  --task 'Reply with exactly CLAUDE_BACKEND_OK and no other text.'
```
