# 2026-08-29 Commit Split Plan

Goal: split the current agent-diff ROI dirty worktree into reviewable commits.

## Proposed Commits

- [ ] Cloudflare durable run service
  - run-session Durable Object
  - live event append/read/SSE
  - token audience split
  - tests and README/config updates
- [ ] Native agent/runtime improvements
  - instruction loading, explicit memory, MCP stdio client
  - cost/model/provider catalog changes
  - tool metadata and read-only parallel execution
  - sandbox profile/read-root configuration and runtime plumbing
  - B4 replay CLI/reducer, A2 allow/policy visibility, B9 pressure fallback
  - focused Python tests
- [ ] Report and execution logs
  - `docs/agent-diff-report.md`
  - `.work/*` execution plans/logs

## Rules

- Stage only files assigned to the current commit.
- Read staged diff before each commit.
- Do not revert unrelated edits.
- Run focused checks before committing where practical.
