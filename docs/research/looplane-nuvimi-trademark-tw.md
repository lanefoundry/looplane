# LOOPLANE / NUVIMI — Taiwan TIPO preliminary trademark screen

- Screen date: 2026-08-22 (Asia/Taipei)
- Official source: [TIPO Trademark Search System](https://cloud.tipo.gov.tw/S282/S282WV1/)
- TIPO database update shown by the interface: 2026-08-02
- Scope: exact, prefix/component, and reasonable Roman-letter similarity screen in Nice Classes 9, 38, and 42
- This is not a legal opinion or guarantee of registrability/non-infringement.

## Decision and ranking

1. **LOOPLANE — PASS WITH CAUTION; advance.** No exact match, no mark beginning with the full `LOOPLANE` string, and no exact `RIVU` prefix mark was found. `RIVIAN` is a meaningful Class 9/42 candidate because it is a strong, established mark sharing the first three letters, but `LOOPLANE` differs at the fourth letter and has a distinct ending and pronunciation. `Risumia` in Class 42 is another record for counsel to inspect, but it is less close overall.
2. **NUVIMI — REJECT.** No exact full-name match, but the candidate begins with the complete active Class 9 mark `NUVI` owned by Garmin. It also sits close to active `NUVEI` across Classes 9, 38, and 42, `NEUMI` in Class 9, and `NuVive` in Class 9. This is an avoidable cluster in the exact software/communications classes.

**Taiwan ranking: `LOOPLANE` >>> `NUVIMI`.** If only these two are under consideration, continue with `LOOPLANE` and stop work on `NUVIMI`.

## Official query method

I queried the public backend used by TIPO's official interface:

1. `POST https://cloud.tipo.gov.tw/S282/S282BV1/api/search/wordSearch`
2. Bodies: `{"tmarkDraft":"LOOPLANE","records":[]}` and `{"tmarkDraft":"NUVIMI","records":[]}`
3. All returned document IDs were passed to `POST /api/result/list`.
4. Results were normalized case-insensitively for exact/full-prefix checks and filtered for class strings containing `009`, `038`, or `042`.
5. Separate `RIVU` and `NUVI` searches tested the distinctive leading components. `RIVU` returned no exact mark; `NUVI` returned the active Garmin Class 9 registration.
6. Pending (`AA`) and active-looking registered (`AB`) records were reviewed. Expired/invalid records were not treated as material active conflicts.

TIPO describes text search as a method to surface marks that may create confusion and recommends checking both text and device/mark data before filing: [TIPO trademark-search guidance](https://www.tipo.gov.tw/tw/tipo1/834-1255.html).

| Candidate | Similarity-pool records | Exact full-name match | Mark beginning with full candidate | Exact leading-component result |
|---|---:|---:|---:|---|
| LOOPLANE | 118 | 0 | 0 | `RIVU`: none |
| NUVIMI | 188 | 0 | 0 | `NUVI`: active Class 9 registration |

The pool counts are not legal risk scores. The records below drive the decision.

## LOOPLANE material records

| Returned mark | Class | Application | Registration | Status | Deadline | Owner | Preliminary relevance |
|---|---|---|---|---|---|---|---|
| **RIVIAN** | **006, 007, 009, 011, 035, 036, 037, 039, 040, 041, 042, 045** | **110043711** | **02230703** | Active-looking | 2032-06-15 | RIVIAN IP HOLDINGS, LLC | Medium: prominent mark in both Classes 9 and 42; shares `RIV-`, but diverges at letter 4 and in ending/sound. |
| RIVIAN | 009, 012 | 109020562 | 02109926 | Active-looking | 2030-12-15 | RIVIAN IP HOLDINGS, LLC | Same family; Class 9. |
| RIVIAN | 009, 012 | 111001782 | 02263996 | Active-looking | 2032-11-15 | RIVIAN IP HOLDINGS, LLC | Same family; Class 9. |
| RIVIAN R1X / R1S / R1T | 009 and other classes | 111034234–111034236 | 02292606–02292608 | Active-looking | 2033-04-15 | RIVIAN IP HOLDINGS, LLC | Reinforces the active `RIVIAN` family. |
| **Risumia** | **042** | **106045723** | **01896491** | Active-looking | 2028-01-31 | 長沙妙傳信息技術有限公司 | Medium-low: similar six/seven-letter rhythm and `Ri-u-mi-a` pattern, but different consonants and ending. |
| RITMIX | 009 | 103009363 | 01695026 | Active-looking | 2035-02-28 | YUKIO YAMANO | Low: shares `RI` and `MI`, but visually and phonetically distinguishable. |
| Riquiy | 009, 042 | 106041875 | 01907801 | Active-looking | 2028-03-31 | SHARP CORPORATION | Low: returned by TIPO, but overall word structure is materially different. |

### LOOPLANE assessment

`LOOPLANE` is not risk-free because `RIVIAN` is a strong, multi-registration mark in the relevant classes. Still, it does not copy the full `RIVI-` prefix: `LOOPLANE` is `RIVU-`, and the words have different endings and likely different cadence. On this preliminary Taiwan word screen, that supports **pass with caution**, subject to full goods-description and pronunciation analysis by counsel.

## NUVIMI material records

| Returned mark | Class | Application | Registration | Status | Deadline | Owner | Preliminary relevance |
|---|---|---|---|---|---|---|---|
| **NUVI** | **009** | **095026284** | **01243838** | Active-looking | 2026-12-31 | **GARMIN SWITZERLAND GMBH** | **High: NUVIMI begins with the entire registered word `NUVI`; same core software/electronic-goods class.** |
| **NUVEI** | **009, 035, 036, 037, 038, 039, 041, 042, 045** | **113020528** | **02505497** | Active-looking | 2035-12-31 | **NUVEI CORPORATION** | **High: one compact five-letter mark close to the `NUVI-` core, covering all three priority classes.** |
| **NEUMI** | **009** | **112021623** | **02334637** | Active-looking | 2033-11-15 | SHENZHEN ELEBAO TECHNOLOGY CO., LTD | High-medium: same five-letter ending/rhythm and only a small vowel/consonant change from `NUVIMI`'s core. |
| NuVive | 009 | 109035590 | 02103973 | Active-looking | 2030-11-30 | 晶碩光學股份有限公司 | Medium-high: shares the complete `NUVI` opening; Class 9. |
| NuView | 009 | 109035589 | 02103972 | Active-looking | 2030-11-30 | 晶碩光學股份有限公司 | Medium: shares `NUV-` and similar visual construction; Class 9. |
| numi | 042 | 115029952 | — | Pending | — | 森芮雅生醫有限公司 | Medium: short component embedded in `NUVIMI`; Class 42. |
| numii | 042 | 115041867 | — | Pending | — | 森芮雅生醫有限公司 | Medium: similar short word/rhythm; Class 42. |
| Nuvari / nuvari設計字 | 009 | 115037363 / 115044910 | — | Pending | — | 數據科技股份有限公司 | Medium: recent `Nuvi-/Nuva-` family in Class 9. |

### NUVIMI assessment

The active `NUVI` registration is decisive for this preliminary screen: `NUVIMI` simply appends `MI` to the entire existing mark in the same core class. `NUVEI` then adds a separate, newer registration spanning Classes 9, 38, and 42. Even if a legal analysis might ultimately distinguish particular goods, the naming field is needlessly crowded. **Reject.**

## Why Classes 9, 38, and 42 matter

TIPO materials place downloadable software, programs, and mobile applications in Class 9 and hosted software/SaaS/PaaS/software engineering/AI consulting in Class 42: [TIPO Class 9 and 42 example](https://www.tipo.gov.tw/tw/tipo1/424-80944.html).

Class 38 is relevant if the product provides messaging, transmission, telecommunications, or communication services. It may not be necessary for a local-only CLI, but the broad `NUVEI` registration covers it alongside Classes 9 and 42.

## Limits

This official-database screen does not decide legal likelihood of confusion and does not exhaust device/logo similarity, Chinese transliterations, exact goods/service descriptions, international priority and families, company/trade names, unregistered reputation, or applications filed after the displayed 2026-08-02 cutoff.

TIPO notes that new filings may take days and longer reconciliation periods to appear. A professional clearance should inspect full case reports and compare marketplace channels, pronunciation, and designated goods/services—especially the `RIVIAN` family for `LOOPLANE`, and `NUVI`/`NUVEI` for `NUVIMI`.

## Final recommendation

- **LOOPLANE: PASS WITH CAUTION** for the next package/domain/global screen.
- **NUVIMI: REJECT** and do not invest further.
