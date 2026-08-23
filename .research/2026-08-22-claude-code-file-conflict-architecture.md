# Claude Code file-conflict architecture

Date: 2026-08-22

## Conclusion

Claude Code 2.1.239 uses per-target optimistic concurrency for file writes. It does
not impose PCA's conversation-lifetime, whole-source-tree equality invariant.
Unrelated source changes therefore do not fail a Claude Code session, while PCA
currently converts any source-tree drift into `source filesystem changed` during
review or cleanup.

## Primary artifact

- Launcher: `/Users/xiaoxu/.local/bin/claude`
- Installed bundle: `/Users/xiaoxu/.local/share/claude/versions/2.1.239`
- Version: `2.1.239 (Claude Code)`
- Embedded build: `9bf8e9521fe06414183309865310e27c9b8db3dd`, built
  `2026-08-21T04:40:30Z`
- Read depth: targeted static trace of the Bun-compiled/minified JavaScript
  bundle, plus CLI contract inspection. This is the installed official artifact,
  not an upstream TypeScript checkout.

## Confirmed behavior

1. `Read` stores content, a content hash, and floored `mtimeMs` in a path-keyed
   `readFileState` cache.
2. `Edit` and `Write` inspect only the target path immediately before mutation.
3. A newer mtime triggers a content comparison. A true conflict returns
   `File has been modified since read... Read it again before attempting to write it.`
4. `Edit` can recover when its `old_string` still applies uniquely and the guard
   allows recovery; its result is marked `staleRecovered`.
5. Bash mutation detection revisits only previously-read files and emits a reread
   hint; it does not fail the whole session.
6. Foreground sessions use the launch cwd. `--worktree` / `EnterWorktree` is
   opt-in. Managed worktree cleanup checks that worktree's Git dirty/ahead state,
   not byte equality of the original checkout.

Relevant bundle anchors and approximate executable offsets:

- Read state write: `tengu_session_file_read`, ~298,691,348
- Content hash/cache: `Bun.hash`, `readFileState`, ~289,040,192
- Edit stale validation: `tengu_edit_tool_stale_read`, ~293,801,024
- Write final target recheck: `Q4v`, `e3v`, ~293,804,039
- Bash previously-read-file notice: `NUT`, ~298,640,660

Exact bundle searches found no `source filesystem changed`, `source_invariant`,
`repo snapshot`, or `filesystem snapshot`. That negative evidence is not a formal
proof by itself; the positive Edit/Write control flow establishes the different
concurrency boundary.

## PCA comparison

- `conversation_workspace.py:172-179` captures a complete filesystem snapshot.
- `conversation_workspace.py:256-290` rehashes the source and returns
  `source filesystem changed` for any mismatch.
- `conversation_workspace.py:479-550` walks everything except the root `.git`
  and hashes every regular file, so ignored/untracked/cache files are included.
- `claude_conversation.py:156-164` and `codex_conversation.py:155-163` repeat the
  source invariant check during close.

This means `.DS_Store`, caches, editors, tests, or another agent touching an
unrelated file can invalidate a PCA conversation. Claude Code's target-file check
would not consider those unrelated paths.

## Design implication

Preserve conflict prevention at the mutation boundary, but remove whole-repo
byte equality as a terminal success condition. Review/apply should validate the
disposable workspace patch and the specific destination paths it will overwrite;
cleanup should remain best-effort and must not retroactively turn a completed
turn into failure because an unrelated source file changed.

## Limitations

No Groundlane MCP tool was exposed in this session, so no web lookup was attempted.
The result is based on the locally installed official bundle and local PCA source.
