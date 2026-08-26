# LUTUNO — Taiwan TIPO preliminary trademark screen

- Screen date: 2026-08-22 (Asia/Taipei)
- Official database: [TIPO Trademark Search System](https://cloud.tipo.gov.tw/S282/S282WV1/)
- Database update shown by TIPO: 2026-08-02
- Scope: `LUTUNO` exact and Roman-letter similarity screen, focused on Nice Classes 9, 38, and 42

## Decision: REJECT

`LUTUNO` has no exact case-insensitive Taiwan result, but it is **not a clean survivor**. The official TIPO similarity pool contains:

- pending `LUNO 及圖` in Class 42;
- active `LUNOS` registrations in Class 9;
- pending `lunfon` in Class 9;
- active `LUTINA` and `Lutron` marks in Class 9.

The most material records are `LUNO` and `LUNOS`: both are only a short edit away from `LUTUNO`, and together occupy the two primary software classes. This is substantially worse than the earlier `Lutrimi` screen. Do not advance `LUTUNO` as the coding-agent name.

## Official query method

I queried the public backend used by TIPO's official interface:

1. `POST https://cloud.tipo.gov.tw/S282/S282BV1/api/search/wordSearch`
2. Request body: `{"tmarkDraft":"LUTUNO","records":[]}`
3. TIPO returned 215 similarity-pool records.
4. All returned document IDs were submitted to `POST /api/result/list`.
5. Results were normalized case-insensitively for an exact-name check, then filtered for class strings containing `009`, `038`, or `042`.
6. Pending (`AA`) and active-looking registered (`AB`) records were reviewed; registrations whose displayed deadlines had already passed by the screen date were not treated as material active hits.

TIPO describes the text search as a tool for finding marks that may pose confusion risk. It also recommends checking text and device/mark data before filing: [TIPO trademark-search guidance](https://www.tipo.gov.tw/tw/tipo1/834-1255.html).

## Exact result

No case-insensitive exact `LUTUNO` word-mark match appeared in the 215-record pool.

That negative exact result does not outweigh the close software-class records below.

## Material Classes 9 and 42 records

| Returned mark | Class | Application | Registration | TIPO pool status | Deadline | Owner | Preliminary relevance |
|---|---|---|---|---|---|---|---|
| **LUNO 及圖** | **006, 011, 019, 037, 042** | **114060499** | — | **Pending** | — | 春和有限公司 | High: `LUTUNO` adds only `TU` internally to `LUNO`; directly relevant Class 42. |
| **lunfon** | **009** | **115055337** | — | **Pending** | — | 吳鎵豪 | Medium: similar length, `lun-` opening and `-on/-o` sound structure; directly relevant Class 9. |
| **LUNOS** | **009, 017** | **104061993** | **01776534** | Active-looking registration | 2036-06-15 | 魯米科技有限公司 | High: six-letter `LUNOS` versus six-letter `LUTUNO`, sharing `LU-NO`; directly relevant Class 9. |
| **LUNOS** | **009, 034** | **113010858** | **02459942** | Active-looking registration | 2035-05-31 | JUUL LABS, INC. | High for the same reason; separate active-looking Class 9 registration. |
| Lutron 及圖 | 009 | 100045321 | 01524515 | Active-looking registration | 2032-06-30 | LUTRON ELECTRONIC ENTERPRISE CO., LTD | Medium: shared `LUT-` opening and similar consonant sequence. |
| LUTINA | 009 | 106020436 | 01879570 | Active-looking registration | 2027-11-15 | TOKAI OPTICAL CO., LTD. | Medium: same six-letter length and `LUT-N-` structure. |

No material active or pending Class 38 record close enough to change the decision was found in this query. The rejection is driven by Classes 9 and 42.

## Why these classes matter

TIPO materials place downloadable software, computer programs, and mobile applications in Class 9, while hosted software, SaaS/PaaS, software engineering, and AI consulting commonly fall in Class 42: [TIPO Class 9 and 42 example](https://www.tipo.gov.tw/tw/tipo1/424-80944.html).

Class 38 would matter if the product itself provides telecommunications, messaging, transmission, or communication services; it is less central for a local coding CLI.

## Limits

This is a preliminary official-database screen, not a legal opinion or guarantee of non-infringement/registrability. It does not determine legal likelihood of confusion, and it does not exhaust:

- device/logo similarity;
- Chinese transliterations or phonetic variants;
- full goods/service descriptions and market channels;
- international priority or trademark-family claims;
- company/trade-name rights and unregistered reputation; or
- applications filed after TIPO's displayed 2026-08-02 database cutoff or not yet reconciled.

TIPO states that newly filed matters can take days or longer reconciliation periods to appear. A professional clearance would need the full case reports for `LUNO` application 114060499, `lunfon` application 115055337, and both active `LUNOS` registrations.

## Final recommendation

**Reject `LUTUNO`.** It is pronounceable, but the Taiwan software-class field is too crowded around `LUNO`/`LUNOS`/`LUT-` to justify investing in it. Continue with a more structurally distinctive coined name; among the previously screened options, `Lutrimi` remains cleaner in Taiwan.
