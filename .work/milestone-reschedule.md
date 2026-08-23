# Milestone reschedule: dependency-ordered delivery

Date: 2026-08-23

## Dependency chain (must hold)

```text
M10/M11  ──complete article review + commits──▶  M12  ──stable lazy-discovery
(rename +                                                      contract──▶  M13
unified conversation)                                          (external
                                                             runtimes)
```

Rationale (from `.research/2026-08-22-capability-current-state-audit.md`):
- M10/M11 are the current uncommitted worktree (rename `coding_agent` → `rivumi` plus unified
  conversation). They must close first so the rename and conversation contract are immutable before
  any performance or runtime work builds on top.
- M12 (startup performance) must precede M13 (OpenCode/Pi/OMP). Otherwise each new eagerly-imported
  adapter enlarges an already expensive startup graph and makes the later lazy-refactor harder. M12
  establishes the lazy discovery + benchmark contract that M13 adapters plug into.

## 1. M10 / M11 — article review and commits

- [ ] Verify full release gates green on the current worktree (pytest / ruff / lock / build / CLI /
      Cloudflare tests).
- [ ] Review the in-repo stage records (`docs/stages/m10-*.md`, `docs/stages/m11-*.md`) for accuracy
      against the implementation; the external QuidProQuo blog drafts are reviewed by the user in the
      `quidproquo` repo and are out of scope here.
- [ ] Create scoped commits preserving unrelated worktree changes:
  - rename `coding_agent` → `rivumi` (src, tests, scripts, config, docs references);
  - M10 runtime-first subscription TUI (implementation + stage doc);
  - M11 unified native conversation (implementation + stage doc);
  - research/work artifacts and startup playbook;
  - `progress.md` milestone update.
- [ ] Close M10/M11 in `progress.md` and update the active/planned milestone markers.

## 2. M12 — measured startup performance

Follow `.work/m12-startup-performance-plan.md` and `docs/startup-performance-playbook.md`:

- [ ] Slice 1: freeze baseline with `scripts/bench_startup.sh` (hyperfine + importtime), retain raw
      JSON under `.artifacts/startup/`.
- [ ] Slice 2: lazy-load path-specific deps (Codex OAuth/OpenAI SDK, vendor backends, conversation,
      gateway, uvicorn) behind narrow loaders; assert lightweight routes no longer import them.
- [ ] Startup telemetry via `RIVUMI_STARTUP_LOG` (bounded, private, non-secret, disabled by default).
- [ ] Slice 3: parallelize independent startup work only after writing the dependency graph.
- [ ] Slice 4: versioned single-flight disk cache for repeated discovery; never cache secrets/failures.
- [ ] Slice 5: CI regression gate after a stable runner baseline (fail >10% median regression).
- [ ] Close M12 with before/after distributions, regression gates, and complete commits.

Acceptance: common CLI routes do not import OpenAI SDK / vendor runtimes / Textual / uvicorn; every
change has paired before/after evidence and functional regression tests.

## 3. M13 — extensible external coding CLI runtimes

Follow `.work/m13-external-coding-cli-adapters-plan.md`:

- [ ] Slice 1: extract/generalize `ConversationRuntimeSession`; capability matrix consumed by
      controller + TUI; remove Claude/Codex-only assumptions in `cli.py` and `conversation.py`.
- [ ] Slice 2: OpenCode adapter (structured SDK/RPC/ACP/JSONL preferred).
- [ ] Slice 3: Pi adapter (stable upstream contract only).
- [ ] Slice 4: OMP (Oh My Pi) adapter; reuse Pi only where protocol evidence proves compatibility.
- [ ] Slice 5: product surface (runtime picker lists Rivumi Agent, Claude Code, Codex CLI, OpenCode,
      Pi, OMP), fake-CLI contract suites, opt-in live smokes.

Architecture boundary (non-negotiable): Rivumi Agent stays the native harness; external CLIs are
sibling runtimes, never the implementation underneath Rivumi Agent or a `ModelProvider` transport.

## Out of scope for this pass

- Native long-lived model conversation / context compaction for Rivumi Agent (separate future work).
- Rewriting Rivumi in another language; absolute cross-machine startup SLA.
- Sharing/importing another CLI's credentials into native mode.
