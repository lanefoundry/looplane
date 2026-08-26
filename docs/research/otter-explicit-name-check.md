# Explicit otter name collision check

Checked 2026-08-22 against the official PyPI JSON API, npm registry, and GitHub repository search API. Matching is case-insensitive. A 404 means no exact registry package existed at check time; it does not reserve the name or establish trademark clearance.

| Candidate | PyPI | npm | Exact GitHub repository | Practical screen |
|---|---:|---:|---|---|
| `PatchOtter` | 404 | 404 | none | **Best clean literal candidate** |
| `OtterPatch` | 404 | 404 | [`Eilen6316/otterpatch`](https://github.com/Eilen6316/otterpatch) | **Avoid**: an active, agent-driven patch product in an adjacent category |
| `OtterForge` | 404 | 404 | none | Usable with caution: an `OtterForge` GitHub account/namespace already owns [`otter-components`](https://github.com/OtterForge/otter-components) |
| `OtterPilot` | 404 | 404 | [`awjc/Otterpilot`](https://github.com/awjc/Otterpilot), [`Shroffie-Dev/otterpilot`](https://github.com/Shroffie-Dev/otterpilot) | Avoid exact project name |
| `OtterFix` | 404 | 404 | none | Relatively clean; one fuzzy repo named `otterfixthing` |
| `OtterDev` | 404 | 404 | [`Hasiful/Otterdev`](https://github.com/Hasiful/Otterdev) | Avoid exact project name; also generic |
| `OtterKit` | 404 | **taken** | three exact repos, including [`otterkit/otterkit`](https://github.com/otterkit/otterkit) | **Avoid**: npm `otterkit` is an AI-agent tunnel CLI |
| `OttrPatch` | 404 | 404 | none | Clean but looks misspelled and is harder to say/search |
| `OtterlyCode` | 404 | 404 | [`OtterlyCode/OtterlyCode`](https://github.com/OtterlyCode/OtterlyCode) | Avoid exact project name |

## Recommendation

Use **PatchOtter** if the name must visibly and immediately communicate “water otter + code patches.” It is clearer than metaphorical names and had no exact PyPI, npm, or GitHub repository collision in this check. **OtterFix** is the next-best clean literal option, but sounds more like a repair utility than an autonomous coding agent.

Registry evidence:

- PyPI: `https://pypi.org/pypi/<normalized-name>/json`
- npm: `https://registry.npmjs.org/<normalized-name>`
- GitHub: authenticated `GET /search/repositories?q=<name>+in:name`, followed by exact case-insensitive repository-name filtering
