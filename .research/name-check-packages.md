# Package registry name check (2026-08-22)

Checked the official PyPI JSON endpoint (`/pypi/<name>/json`) and npm registry
endpoint (`registry.npmjs.org/<name>`). HTTP 404 means there is no current
public project at that exact normalized name; it does **not** guarantee that a
registry will allow a new upload (reserved, prohibited, or recently deleted
names can still be rejected at publish time).

| Candidate | PyPI | npm | Naming assessment |
|---|---|---|---|
| `paca` | **Taken** — 0.1.0, Alpaca Broker API asset-status CLI; released 2021-12-24 | **Taken** — 1.0.10 CLI; released 2016-08-16 | Do not use as the distribution/package name |
| `paca-code` | 404 | 404 | Strong candidate; publish-time check still required |
| `pacacode` | 404 | 404 | Available-looking, but less readable than `paca-code` |
| `codepaca` | 404 | 404 | Strong candidate and distinctive |
| `paca-agent` | 404 | 404 | Available-looking; product scope is explicit |
| `paca-cli` | 404 | 404 | Available-looking; unnecessarily ties the brand to CLI |
| `pocket-python` | 404 | 404 | Available-looking; generic phrase and may imply a Python runtime |
| `pocketpython` | 404 | 404 | Available-looking; generic and less readable |
| `otter` | 404 | **Taken** — 0.1.2, server-runs-client-apps tool; released 2013-03-02 | Do not use as a cross-ecosystem package name |
| `otter-agent` | 404 | 404 | Available-looking, but generic |
| `capybara` | **Taken** — 0.2.0, multi-API-token wrapper; released 2015-06-08 | **Taken/deprecated** — 0.1.2 CSS tool; released 2017-01-30 | Do not use as the distribution/package name |
| `axolotl` | **Taken/active** — 0.18.0, LLM Trainer; released 2026-07-17 | **Taken** — 1.3.0, forward-secrecy protocol; released 2015-02-19 | Strong conflict in the same AI/Python category; avoid |
| `pybara` | 404 | 404 | Distinctive and short, but pronunciation/animal association is less immediate |

## Registry-backed shortlist

1. `paca-code` — clearest readable name and currently 404 in both registries.
2. `codepaca` — more brand-like/distinctive and currently 404 in both registries.
3. `paca-agent` — clearest product category, though narrower if the product later
   expands beyond an agent.
4. `pybara` — technically clean in both registries, but weaker semantic fit.

`paca` can still be the mascot or informal product shorthand, but it should not
be the PyPI/npm distribution name because both registries already contain a
public `paca` package. The existing Python import module (`coding_agent`) and CLI
command (`pca`) do not have to match the eventual distribution/brand name.

## Official endpoints

- PyPI: `https://pypi.org/pypi/<candidate>/json`
- npm: `https://registry.npmjs.org/<candidate>`

