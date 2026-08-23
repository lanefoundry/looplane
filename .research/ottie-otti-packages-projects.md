# Ottie / Otti preliminary package and project name clearance

Queried: 2026-08-22 08:26-08:35 UTC (2026-08-22 16:26-16:35 Asia/Taipei)

Scope: exact package names in common public registries, GitHub account/repository collisions, and notable software/products/companies. This is a practical collision check, not a legal trademark opinion or a substitute for jurisdiction-specific counsel.

## Bottom line

- **Ottie: reject for an AI/coding-agent product.** An active exact-name project already describes itself as an AI agent runtime: [jiayaoqijia/Ottie](https://github.com/jiayaoqijia/Ottie) / [ottie.xyz](https://ottie.xyz). This is not merely a fuzzy consumer-brand collision; it is the same AI-agent category.
- **Otti: reject as the main software brand.** Exact-name software already includes [Otti](https://otti.com/) (a SaaS performance-management platform), an exact-name terminal OTP application, and an exact-name zkSNARK compiler. The GitHub handle is also occupied.
- Exact unscoped package identifiers were unclaimed in the four registries checked at the query time, but that does **not** rescue either brand: project/product collisions are materially stronger than package availability.

## Official package registries

| Exact name | PyPI | npm (unscoped) | crates.io | RubyGems |
|---|---|---|---|---|
| `ottie` | 404, no distribution returned | 404, no package returned | 404, `crate ottie does not exist` | 404, no gem returned |
| `otti` | 404, no distribution returned | 404, no package returned | 404, `crate otti does not exist` | 404, no gem returned |

Primary query endpoints:

- PyPI: [ottie JSON](https://pypi.org/pypi/ottie/json), [otti JSON](https://pypi.org/pypi/otti/json)
- npm: [ottie](https://registry.npmjs.org/ottie), [otti](https://registry.npmjs.org/otti)
- crates.io: [ottie](https://crates.io/api/v1/crates/ottie), [otti](https://crates.io/api/v1/crates/otti)
- RubyGems: [ottie](https://rubygems.org/api/v1/gems/ottie.json), [otti](https://rubygems.org/api/v1/gems/otti.json)

Interpretation: a 404 only says the exact public identifier was not returned at that moment. It does not prove the name is registrable, reserved, safe under platform policy, or legally clear. Scoped npm packages and private registries were outside this check.

## GitHub and open-source project collisions

### Ottie

1. **Direct, high-risk AI category collision:** [jiayaoqijia/Ottie](https://github.com/jiayaoqijia/Ottie) calls itself “The agent runtime that proves what it did” and an AI agent with auditable actions. Repository metadata at query time: 32 stars, created 2026-03-13, last push 2026-08-22, AGPL-3.0. Its README links [ottie.xyz](https://ottie.xyz). This is active and directly adjacent to an AI coding agent.
2. [domdomegg/ottie](https://github.com/domdomegg/ottie) is OTTIE, the “Online Teaching Type Inference Environment,” a programming/type-inference teaching web app. Repository metadata: 23 stars; active push 2026-08-11.
3. [Ottie-ai-im/ottie](https://github.com/Ottie-ai-im/ottie) is another exact-name repository under an AI-labelled account; [ottie-im/ottie-agent](https://github.com/ottie-im/ottie-agent) is an archived exact `ottie-agent` repository. These are weaker individually, but add search and namespace confusion.
4. The exact GitHub handle [`ottie`](https://github.com/ottie) is occupied, so `github.com/ottie/...` cannot be the project's organization URL unless acquired/transferred.

GitHub repository search for `ottie in:name` returned 59 results at query time. The count is only a search-pollution indicator; the exact-name projects above are the material evidence.

### Otti

1. [dnaka91/otti](https://github.com/dnaka91/otti) is an exact-name terminal/TUI one-time-password manager. Its GitHub mirror is archived because development moved to the author's forge, but it remains distributed through the [Arch User Repository as `otti`](https://aur.archlinux.org/packages/otti).
2. [eniac/otti](https://github.com/eniac/otti) is an exact-name zkSNARK compiler, solver, prover, and verifier for optimization problems, associated with the [Otti research paper](https://eprint.iacr.org/2021/1436).
3. The exact GitHub handle [`otti`](https://github.com/otti) is occupied.

GitHub repository search for `otti in:name` returned 768 results at query time. Many are irrelevant substring matches (for example Italian words beginning with `otti`), so the count should not be treated as 768 competing products; the two exact software projects are sufficient to establish collision.

## Notable products and companies

### Ottie

- [Ottie AI](https://www.withottie.ai/) is an active AI mediator for couples and has an [App Store listing](https://apps.apple.com/gr/app/ottie-ai-couples-counseling/id6754530245). It is a different use case, but occupies the exact “Ottie AI” product wording.
- The active [Ottie agent runtime](https://github.com/jiayaoqijia/Ottie) is the decisive collision because both products would live in the AI-agent/developer ecosystem.

### Otti

- [Otti](https://otti.com/) is an existing software/SaaS company for performance management and manager workflows. Its [terms](https://otti.com/terms) explicitly identify the Otti name, logo, platform and related service marks as its property. This is a primary-source ownership claim, not confirmation of a particular trademark registration.
- [otti SaaS](https://otti.omeron.com/) is a separate travel-management software product with booking, task, notification and related SaaS tooling, plus an [official Chrome extension](https://chromewebstore.google.com/detail/otti-saas-assistant/amkehjeibmaoocigmpmdfffgmjndgpah).

## Practical recommendation

Do not use `Ottie`, `Ottie AI`, or `Otti` as the main project/product name. Even though the exact PyPI/npm/crates.io/RubyGems identifiers appeared unclaimed, the active **Ottie AI agent runtime** creates an immediate same-category conflict, while **Otti** already has multiple software meanings and an established SaaS company. A more distinctive coined name should be checked across package registries, GitHub, search engines, domains, app stores, and official trademark databases before adoption.

## Method notes

- Registry status was queried directly against official JSON/API endpoints with `curl`.
- GitHub repository and account metadata was queried through the official GitHub API/CLI; README claims were read from the repositories themselves.
- Product checks prioritized official websites, official repository READMEs, and official app-store pages. Search-engine snippets were used only to discover primary sources.
- No conclusion here asserts legal clearance. A separate trademark search should cover the intended markets and relevant classes, especially downloadable software and SaaS.
