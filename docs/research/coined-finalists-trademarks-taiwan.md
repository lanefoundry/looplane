# Coined finalists: Taiwan preliminary trademark screen

- Screen date: 2026-08-22 (Asia/Taipei)
- Candidates: `Lutrimi`, `Lutrilo`, `Lutruna`, `Otrilo`
- Focus: exact and meaningful Roman-letter candidates in Taiwan, especially Nice Classes 9, 38, and 42
- TIPO database update shown by the official system: 2026-08-02
- This is a preliminary naming screen, not a legal clearance opinion or registrability guarantee.

## Ranked survivors

1. **Lutrimi — strongest survivor.** No exact Taiwan match. Only one active-looking Class 9 hit appeared in the TIPO similarity pool (`Lutronic`), and it is materially longer and visually distinct. No indexed exact Taiwan-facing product/company collision was found.
2. **Lutrilo — usable with professional review.** No exact match, but the newly pending `LUNTRON` Class 42 application and active `Lutron`/`Lutronic` Class 9 marks share the `LUTR-/LUNTR-` opening. The other returned software-class hits are less meaningful.
3. **Lutruna — caution.** No exact match, but the pool contains pending `LUNTRON` in Class 42, active `LUTINA` in Class 9, and active `LUTRADUR` in Class 38. `LUTINA` is especially close in length and structure.
4. **Otrilo — drop.** No exact match, but TIPO contains a 2026 pending `Orito` application covering Classes 9 and 42. It differs by only one internal letter and targets the same software classes, making this the clearest material risk among the four.

**Recommended Taiwan order: `Lutrimi` > `Lutrilo` > `Lutruna` >>> `Otrilo`.**

## Official query method

I used the public backend of the official [TIPO Trademark Search System](https://cloud.tipo.gov.tw/S282/S282WV1/):

1. `POST https://cloud.tipo.gov.tw/S282/S282BV1/api/search/wordSearch`
2. Body: `{"tmarkDraft":"<UPPERCASE NAME>","records":[]}`
3. All returned document IDs were submitted to `POST /api/result/list`.
4. Results were normalized case-insensitively for exact matches, then filtered for class strings containing `009`, `038`, or `042`.
5. Active/pending candidates were identified from TIPO dataset types `AA` (pending/applied pool) and `AB` (registered/active pool), with registration deadlines recorded where supplied.

The official system describes this as a text-similarity search intended to surface marks that may create confusion. TIPO recommends checking both text and device/mark data before filing: [TIPO trademark-search guidance](https://www.tipo.gov.tw/tw/tipo1/834-1255.html).

| Query | Total similarity-pool records | Exact case-insensitive matches | Class 9/38/42 records (all statuses) |
|---|---:|---:|---:|
| LUTRIMI | 60 | 0 | 3 |
| LUTRILO | 147 | 0 | 11 |
| LUTRUNA | 109 | 0 | 10 |
| OTRILO | 250 | 0 | 30 |

Raw pool size is not itself a legal risk score; the material hits below matter more.

## Material Taiwan records

### Lutrimi

| Returned mark | Class | Application | Registration | Status | Deadline | Owner |
|---|---|---|---|---|---|---|
| Lutronic | 007, 008, 009 | 102028266 | 01638915 | Active-looking registration | 2034-04-15 | LUTRONIC HOLDING GMBH |

Assessment: low preliminary concern. `Lutronic` shares the opening `Lutr-`, but the full words, endings, length, and likely pronunciation are materially different. A professional search should still consider the goods description and owner arguments.

### Lutrilo

| Returned mark | Class | Application | Registration | Status | Deadline | Owner |
|---|---|---|---|---|---|---|
| LUNTRON | 042 | 115015787 | — | Pending | — | 倫創控股有限公司 |
| Lutron 及圖 | 009 | 100045321 | 01524515 | Active-looking registration | 2032-06-30 | LUTRON ELECTRONIC ENTERPRISE CO., LTD |
| Lutronic | 007, 008, 009 | 102028266 | 01638915 | Active-looking registration | 2034-04-15 | LUTRONIC HOLDING GMBH |

Assessment: moderate preliminary concern, primarily because `LUNTRON` is a fresh Class 42 application and all three marks share a similar opening. `Lutrilo` remains more distinct than `Lutruna` and `Otrilo`.

### Lutruna

| Returned mark | Class | Application | Registration | Status | Deadline | Owner |
|---|---|---|---|---|---|---|
| LUNTRON | 042 | 115015787 | — | Pending | — | 倫創控股有限公司 |
| LUTRADUR | 038 | 069030313 | 00156751 | Active-looking registration | 2031-07-31 | CARL FREUDENBERG KG |
| Lutron 及圖 | 009 | 100045321 | 01524515 | Active-looking registration | 2032-06-30 | LUTRON ELECTRONIC ENTERPRISE CO., LTD |
| Lutronic | 007, 008, 009 | 102028266 | 01638915 | Active-looking registration | 2034-04-15 | LUTRONIC HOLDING GMBH |
| LUTINA | 009 | 106020436 | 01879570 | Active-looking registration | 2027-11-15 | TOKAI OPTICAL CO., LTD. |

Assessment: medium-to-high preliminary concern. `LUTINA` is a six-letter word with the same `LUT-` start and `-NA` ending, and `LUNTRON` overlaps heavily in consonant structure in the directly relevant Class 42.

### Otrilo

| Returned mark | Class | Application | Registration | Status | Deadline | Owner |
|---|---|---|---|---|---|---|
| **Orito** | **009, 042** | **115043185** | — | **Pending** | — | CYBOZU, INC. |
| O-trim | 009 | 095042678 | 01264399 | Active-looking registration | 2027-05-31 | 凌耀科技股份有限公司 |
| orglo | 009 | 109079279 | 02133867 | Active-looking registration | 2031-04-15 | SHENZHEN YUNDING INFORMATION TECHNOLOGY CO., LTD. |
| orglo | 042 | 109079285 | 02136276 | Active-looking registration | 2031-04-15 | Same owner |
| OSTRO | 009 | 111000897 | 02242523 | Active-looking registration | 2032-08-15 | NOKIA CORPORATION |

Assessment: high concern. `Orito` versus `Otrilo` differs only by insertion of `l`, and the pending application claims both key software classes. The applicant is CYBOZU, INC., an established software company. This is a much more material result than the other broader similarity-pool items.

## Taiwan-facing product and company search

General web searches, Taiwan company-registry-index searches, Taiwan App Store searches, and Google Play searches returned no verified exact product or company hit for `Lutrimi`, `Lutrilo`, `Lutruna`, or `Otrilo` as of the screen date. The only search results for these exact strings were noise such as usernames, OCR fragments, anagrams, or foreign-language text.

This negative result is limited by search-engine indexing. It does not replace a direct company/trade-name registry search, app-store availability test, domain check, package-registry check, or common-law market-use investigation.

## Why Classes 9, 38, and 42 were prioritized

- TIPO materials place downloadable computer software, computer programs, and mobile applications in Class 9.
- Hosted/non-downloadable software, SaaS/PaaS, software engineering, and AI consulting commonly fall in Class 42: [TIPO Class 9 and 42 example](https://www.tipo.gov.tw/tw/tipo1/424-80944.html).
- Class 38 is relevant if the product itself provides telecommunications, messaging, transmission, or communication services; it may be unnecessary for a local-only coding CLI.

TIPO also notes that Class 9 software and Class 42 software/programming services may be closely related in marketplace analysis: [TIPO classification presentation](https://www.tipo.gov.tw/wSite/public/Attachment/007/f1744687679274.pdf).

## Source-depth inventory

| Source | Read level | Use | Limitation |
|---|---|---|---|
| TIPO Trademark Search System UI and public API | First-party, direct query | Exact and similar Taiwan case data | Informational search result; not a legal-status certificate or examiner conclusion. |
| TIPO search guidance | First-party page | Search scope and purpose | General guidance. |
| TIPO Class 9/42 materials | First-party page/material | Software-class relevance | Filing-specific goods/services still need professional drafting. |
| Web/company/app-store search | Search-level negative screen | Exact Taiwan-facing collision check | Absence from search results does not prove absence from the market or registries. |

## Freshness and legal limitations

The official interface displayed a database update date of 2026-08-02. TIPO says new electronic applications may appear after roughly four days and paper filings after roughly ten days, with longer reconciliation periods. Therefore this screen cannot exclude applications filed after the database cutoff or records not yet reconciled.

It also does not cover device/logo similarity, Chinese transliterations, every goods/service description, priority claims, international trademark families, unregistered reputation, company-name rights, or likelihood-of-confusion doctrine. Before filing, counsel should inspect the full case reports—especially `Orito` application 115043185 and `LUNTRON` application 115015787—and run phonetic/transliteration variants.

## Recommendation

Advance **Lutrimi** to the next global/package/domain screen. Keep **Lutrilo** only as a backup. Do not invest further in **Otrilo**; place **Lutruna** behind Lutrilo unless its brand qualities are materially stronger.
