# M5 live subscription coding evidence

Date: 2026-08-21

Both successful runs used a clean Git repository copied from
`evals/fixtures/tiny-python-bug`, an allowlist containing only
`src/tiny_python_bug/calculator.py`, and the exact final command `pytest -q`.
The official external CLI edited PCA's disposable clone. PCA then validated the full Git patch
and executed the final check. The source repositories remained clean and at the exact `base_sha`
stored in each `request.json`.

## Codex CLI

- Run: `cc56e556d8c94dcb865e4bec05b1e0d4`
- Authentication owner: installed official Codex CLI; PCA did not inspect or copy its store.
- External sandbox: `workspace-write`, ephemeral session, user config/rules ignored.
- Result: `completed`, terminal reason `verified`, one allowed changed file, `pytest -q` passed.
- Patch SHA-256: `aabb0491ca737152246996e0d9c1139acfc8fde2083b1c5aa3583f460124e35c`
- Events SHA-256: `cb7f36f48c69ee142f1e58c4a53a10e61089a266f82ec875b7bf50a332b6c30b`
- Checkpoint SHA-256: `6be58e5708a305611e13ebfde4bc256f842eed02d3075dad61dba375a8c63ba1`
- Backend result SHA-256: `8345844a052656ae68823bfd80f3a34effb3dc04b37ce49d9906f3c0328bdf30`
- PCA result SHA-256: `c388b057ef6e890e0d7769732b1f24c076953d1cf8219499197dbffdae668dc6`

Two earlier runs are deliberately retained as fail-closed evidence:

- `a4ce6c30160b4405b45d9f1ac6512282` rejected a tracked bytecode change outside the path
  allowlist. Result SHA-256:
  `434b9aedbe2138145388f13cb3db319139fa7658ed4de8c8d6dcbda1ab507705`.
- `d21351ae798942058585bd201f89903f` rejected untracked bytecode output. Result SHA-256:
  `7b95b2b71b0cde04da4ebd53cc60755a70810a2063ed7ae20d8052af28887c12`.
- `15449cf8ac884f198f7bb75e537738c8` rejected an otherwise correct edit because the Codex JSONL
  stream contained protocol drift. Result SHA-256:
  `0699bbb99152ddc8c46716ba2bfd2a4a9cd4a5ae97bbc04be46fe29dc460f616`.
- PCA then added `PYTHONDONTWRITEBYTECODE=1` to the controlled external process environment and
  reran from a new clean source repository; the successful run above is the post-fix evidence.

## Claude Code

- Run: `aa366fe83d03479eb61c7dfd755b4aa9`
- Authentication owner: installed official Claude Code; PCA did not inspect, copy, or forward the
  child-owned login. This path is local/private and experimental, not a subscription proxy.
- Tools: exact allowlist `Read,Glob,Grep,Edit`; no Bash, Write, WebFetch, WebSearch, MCP, or
  subagent tool was enabled; session persistence and slash commands were disabled.
- Result: `completed`, terminal reason `verified`, one allowed changed file, `pytest -q` passed.
- Patch SHA-256: `aabb0491ca737152246996e0d9c1139acfc8fde2083b1c5aa3583f460124e35c`
- Events SHA-256: `118deb91af25a8e028b14b1d185f331cfb02c4f41c3ff0b005512f14e5884fc8`
- Checkpoint SHA-256: `8b2de6c4bfff4051654c9ffccdfebd5398594d58d17a0e318f005038e91cd0f4`
- Backend result SHA-256: `7903cdbe88e2ebf7c9b347eb5d51d0d2b8d90901880e3e6fd6444cc093709942`
- PCA result SHA-256: `d732a62ff2b1abf90e733189eb803423241afbead95198ff8974837c85c4c10b`

## Boundary

These runs prove local delegated coding through the installed subscription CLIs. They do not
prove that subscription authentication can be relayed to Cloudflare, exposed as a hosted product,
or treated as a `ModelProvider`. Cloudflare deployment remains a separate milestone using API
credentials and a sandbox/container trust boundary.

The release runs above use the final isolation shape: PCA removes `.git` from the child working
tree, keeps Git metadata in a sibling control directory, snapshots all source filesystem entries
except Git internals (including ignored files) before delegation, and rechecks source plus the
reviewable patch after final verification. Codex is invoked with `--skip-git-repo-check` because
the external child deliberately receives no repository metadata.
Every retained release artifact is mode `0600`; text artifacts use same-directory atomic replace
and durable file/directory sync rather than the process umask default.
