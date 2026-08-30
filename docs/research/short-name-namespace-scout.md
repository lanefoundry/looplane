# Short coined-name namespace scout

Queried: 2026-08-22 09:10-09:18 UTC (2026-08-22 17:10-17:18 Asia/Taipei)

Goal: independently generate and quickly screen at most 12 pronounceable 4-6 letter ASCII names for a Python/CLI project. Excluded by request: Ottie, Otti, Lutori, Lutuno, Otrilo.

`stealth_fetch` was not available in this agent's current toolset, so the documented fallback was used: official registry/GitHub APIs plus general web search. No Playwright was used.

## Result

Only **two of twelve** survive this intentionally strict short-name crowding screen:

1. **looplane** — strongest: PyPI/npm clear, GitHub exact user/repository clear, zero fuzzy repository-name results, and no exact software/product/company result surfaced.
2. **Nuvimi** — runner-up: PyPI/npm clear, GitHub exact user/repository clear, only two irrelevant fuzzy repository-name results, and no exact software/product/company result surfaced.

Short coined names are crowded enough that keeping only two is more useful than padding the shortlist with names that already have exact handles, repositories, packages, apps, or products.

## Twelve-name screen

| Candidate | Length / pronunciation | PyPI | npm | GitHub exact handle | GitHub exact repo | Product-search result | Decision |
|---|---|---:|---:|---|---|---|---|
| **looplane** | 6; ri-vu-mi | 404 | 404 | Clear | Clear | No exact named product/company surfaced | **Keep #1** |
| **Nuvimi** | 6; nu-vi-mi | 404 | 404 | Clear | Clear | No exact named product/company surfaced | **Keep #2** |
| Lutelo | 6; lu-te-lo | 404 | 404 | Occupied | Clear | Exact-name music artist on Apple Music | Drop |
| Lutimi | 6; lu-ti-mi | 404 | 404 | Occupied | Clear | Active `LUTIMI NR CORP` company name plus heavy GitHub search noise | Drop |
| Lutiva | 6; lu-ti-va | 404 | 404 | Clear | **Occupied** (`sxy20230301-byte/Lutiva`) | [Lutiva Soluções](https://lutsolint.com.br/) is an IT/process-automation business | Drop |
| Vutri | 5; vu-tri | 404 | 404 | Occupied | **Occupied** | [Vutri Barbershop app](https://play.google.com/store/apps/details?id=com.vutri.client.app) | Drop |
| Nuvlo | 5; nu-vlo | 404 | 404 | Occupied | **Occupied** (2 exact repos) | [Nuvlo entrepreneur platform](https://www.mynuvlo.com/) and exact-name mobile apps | Drop |
| Roveli | 6; ro-ve-li | 404 | 404 | Occupied | Clear | [Roveli marketplace](https://www.roveli.ro/) and several exact-name retail/company uses | Drop |
| Pebio | 5; pe-bi-o | 404 | 404 | Occupied | **Occupied** (`Uase47/PEbio`) | `PEBIO-R` is an established Argentine bioeconomy-program acronym | Drop |
| Tavio | 5; ta-vi-o | 404 | 404 | Occupied | **Occupied** (8 exact repos) | Direct software/AI collisions: [Tavio iPaaS](https://www.tavio.io/) and [Tavio AI API](https://tavio.tech/en/) | Drop |
| Velumi | 6; ve-lu-mi | 404 | **Occupied** | Occupied | **Occupied** | Direct developer/AI collisions: [Velumi European hosting](https://velumi.com/about) and [Velumi AI app](https://www.velumi.ai/) | Drop |
| Torumi | 6; to-ru-mi | 404 | 404 | Occupied | Clear | Exact company/font/LINE-sticker uses; namespace already noisy | Drop |

## Official namespace evidence

Each candidate was queried against:

- PyPI exact distribution JSON: `https://pypi.org/pypi/{name}/json`
- npm exact unscoped package: `https://registry.npmjs.org/{name}`
- GitHub exact user/org: `https://api.github.com/users/{name}`
- GitHub repository search: `{name} in:name`, then case-insensitive exact repository-name filtering

For the two survivors:

| Name | PyPI | npm | GitHub user/org | Exact repository | Fuzzy repository count |
|---|---:|---:|---:|---|---:|
| `looplane` | 404 | 404 | 404 | None | 0 |
| `nuvimi` | 404 | 404 | 404 | None | 2 irrelevant substring results |

These status codes are timestamped observations, not reservations or guarantees. Package and account availability must be refreshed immediately before registration/publication.

## Recommendation

Advance **looplane** and **Nuvimi** to the next phase only. The next phase should cover:

1. exact and phonetic trademark databases in intended markets/classes;
2. `.com`, `.ai`, and practical alternate-domain status;
3. App Store/Google Play exact and phonetic names;
4. social handles and pronunciation checks with English and Chinese speakers.

This is a preliminary technical/product namespace screen, not legal clearance.
