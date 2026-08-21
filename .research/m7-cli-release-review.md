# M7 CLI release review

Date: 2026-08-22

## Initial verdict: NEEDS WORK

Independent read-only review ran the full suite (198 passed at that snapshot) and found:

1. **High — provider switch retained another provider's model/API URL.** Updating only
   `provider` in `pca config` merged the old dependent fields. A later request could therefore
   route a new provider credential to an old custom endpoint.
2. **Medium — headless tool capability was changed from explicit to assumed.** `exec/run` defaulted
   `tool_calling=True`, weakening the established fail-closed contract.
3. **Medium — default-command routing suppressed root shell completion.** Public subcommands were
   not discoverable while completion mode bypassed the hidden chat route.
4. **Medium — realistic legacy `-p PROVIDER --task ...` produced the wrong migration error.**
5. **Low — help and errors exposed the internal `chat` routing command.**

## Corrections

- A provider change now clears saved model/API URL unless each value is explicitly supplied in the
  same config update. Tests reproduce the credential-routing case.
- `exec/run` again defaults `tool_calling=False`; callers explicitly pass `--tool-calling`.
- Completion mode preserves public subcommand discovery. `DefaultCommandGroup.shell_complete()`
  merges the hidden default command's options only for option prefixes and de-duplicates results.
- Legacy `-p PROVIDER` detection runs before positional/`--task` conflict validation.
- `DefaultChatContext` and `DefaultChatCommand` present root usage/help in all routed diagnostics.

## Re-review

The first re-review confirmed the five corrections but found one remaining Medium completion gap:
`pca --`, `pca -p --`, and `pca Fix --` listed only root meta options rather than daily prompt
options. The merged `shell_complete()` implementation and three regression forms closed it.

## Final verdict: GO

The reviewer independently reproduced:

- `pca ` completes `resume`, `config`, `gateway`, `run`, `exec`, `auth`, and `backend`;
- `pca --`, `pca -p --`, and `pca Fix --` complete `--cd`, `--repo`, `--print`, `--provider`,
  `--model`, and `--check` plus root meta options;
- completion results contain no duplicates;
- targeted CLI/config suite: 21 passed.

No release-blocking finding remains in the M7 CLI scope. The reviewer modified no files.
