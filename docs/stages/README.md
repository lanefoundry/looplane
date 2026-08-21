# Milestone records

Each completed milestone gets one reproducible engineering record. These records describe
what the code actually does at a specific commit; they are not aspirational roadmaps.

## Required structure

```markdown
# <Milestone>: <outcome>

## Scope
## Baseline and acceptance criteria
## References studied
## Ideas borrowed
## Adjustments made for this project
## Ideas deliberately not adopted
## Implementation
## Verification evidence
## Known limitations
## Artifact paths
## Commit
```

The references section must name the source project/article and the exact design boundary used.
The adjustments section must explain where the implementation diverges and why. Verification
must include commands and results, not only a statement that testing was performed.

The commit SHA is filled after the milestone commit. If a follow-up correction is needed, append
the corrective commit rather than rewriting the historical result.

## Records

- [M1: Local Python coding-agent harness](m1-local-harness.md)
- [M2: Interactive CLI and provider gateway](m2-interactive-cli-provider-gateway.md)
- [M3: Reliable exact editing and real-provider eval](m3-reliable-editing-real-provider-eval.md)
- [M4: Provider completion and subscription boundaries](m4-provider-completion.md)
- [M5: Subscription-backed external coding](m5-subscription-backed-external-coding.md)
- [M6: Deployed Cloudflare Sandbox service](m6-cloudflare-sandbox-service.md)
- [M7: Familiar CLI ergonomics](m7-familiar-cli-ergonomics.md)
