# Animal project and package naming check

Checked: 2026-08-22

Scope: practical collision screening for a Python coding-agent project,
distribution, and CLI. This is not legal or trademark clearance.

## Recommendation

Use **PatchPaw** as the project/product name if a rename is wanted:

- Product: `PatchPaw`
- PyPI distribution: `patchpaw`
- CLI: `patchpaw`
- Existing compatibility CLI: keep `pca` temporarily
- Python import package: keep `coding_agent`; an import rename is unnecessary
- Mascot/loading animation: the original bubble otter can remain independent of
  the product name

At check time, the exact `patchpaw` name returned 404 from the official PyPI
JSON endpoint and npm registry, and GitHub repository search returned zero exact
name matches. This is evidence of low current collision risk, not a reservation
or guarantee that publication will succeed.

## Candidate assessment

| Name | Assessment | Reason |
|---|---|---|
| `PatchPaw` | Best candidate | Coined, relevant to code patches, animal-friendly, and no exact PyPI/npm/GitHub repository hit at check time |
| `Pybara` | Usable fallback | PyPI/npm exact names are empty-looking and meaningful GitHub collisions are low; the name is less obviously a coding agent |
| `CodePaca` | Use with caution | Exact PyPI/npm/GitHub name is empty-looking, but it remains close to the active Paca AI agent ecosystem |
| `Paca Code` | Avoid | Spoken brand is still Paca, creating direct AI/coding-category confusion |
| `Pocket Python` | Avoid | Generic phrase, existing exact small projects, and close to the established `pocketpy` interpreter |
| `Paca` | Avoid | Exact PyPI/npm names are occupied; Paca-AI is an active adjacent AI-agent project |
| `Otter` | Avoid as product/package | Saturated software name with multiple coding-agent/project collisions; okay as an unnamed mascot species |
| `Capybara` | Avoid | Established Capybara web-testing framework and occupied package handles |
| `Axolotl` | Avoid | Major active Python/AI fine-tuning project and occupied package handles |

## Primary evidence

- Paca AI: <https://github.com/Paca-AI/paca>
- Capybara: <https://github.com/teamcapybara/capybara>
- Axolotl: <https://github.com/axolotl-ai-cloud/axolotl>
- PocketPy: <https://github.com/pocketpy/pocketpy>
- PyPI lookup pattern: `https://pypi.org/pypi/<name>/json`
- npm lookup pattern: `https://registry.npmjs.org/<name>`

## Naming boundary

The mascot does not need to own the package namespace. The loading character
can be a bubble otter while the product is PatchPaw. This avoids forcing a
common animal word into a crowded developer-tool namespace and leaves room for
the mascot to evolve without another package rename.

Before release, repeat the registry checks immediately before publishing and do
a separate domain, social-handle, and trademark search for the target markets.

## Otter-specific alternatives

Follow-up checked: 2026-08-22. Exact-name checks used the official PyPI and npm
registry endpoints plus GitHub repository search.

| Name | PyPI | npm | Exact GitHub repos | Assessment |
|---|---:|---:|---:|---|
| `PatchRaft` | 404 | 404 | 0 | Best otter-specific option. A group of sea otters floating together is a raft; `patch` also explains the coding purpose. Possible secondary association with the Raft consensus algorithm. |
| `PatchLutra` | 404 | 404 | 0 | Distinctive and biologically tied to otters through *Lutra*, but pronunciation and meaning need explanation. |
| `ShellRaft` | 404 | 404 | 0 | Connects terminal shell plus the otter collective noun, but says less about code modification. |
| `PatchPebble` | 404 | 404 | 0 | Cute sea-otter imagery and patch meaning; slightly toy-like and also sounds like a physical repair product. |
| `OtterPatch` | 404 | 404 | 1 | Immediately understandable, but inherits the heavily saturated `Otter` software namespace. |
| `PatchRomp` | 404 | 404 | 0 | A land group of otters can be called a romp and the name feels playful, but that fact is obscure and `romp` has unrelated meanings. |

Primary animal reference: Monterey Bay Aquarium uses both "raftmates" and
"raft" for sea otters:
<https://www.montereybayaquarium.org/about-us/about-the-aquarium/newsroom/press-releases/meet-suri-and-willow-monterey-bay-aquariums-newest-sea-otters>

Updated recommendation when a hidden otter reference is acceptable:

1. `PatchRaft`
2. `PatchLutra`
3. `ShellRaft`

## Superseding recommendation: visibly identifiable as an otter

The user clarified that the project name itself must visibly read as an otter,
not merely contain an otter reference that needs explanation. Under that
requirement, the recommendation changes to **PatchOtter** (`patchotter`):

- Exact PyPI distribution: no public match at check time
- Exact npm package: no public match at check time
- Exact GitHub repository: no match at check time
- Semantics: `Patch` communicates the coding action and `Otter` makes the
  mascot species explicit

`OtterPatch` is not an interchangeable alternative: an existing
[`Eilen6316/otterpatch`](https://github.com/Eilen6316/otterpatch) project is an
agent-driven patch product in an adjacent category. Full screening details are
in `otter-explicit-name-check.md`.
