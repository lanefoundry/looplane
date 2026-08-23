# OTTIE / OTTI Taiwan preliminary name and trademark screen

- Screen date: 2026-08-22 (Asia/Taipei)
- TIPO database update shown by the official system: 2026-08-02
- Scope: preliminary Taiwan word-mark screen, focused on software-related Nice Classes 9 and 42, plus exact-name software/products visible to Taiwan users
- This is not a legal opinion, a clearance report, or a guarantee of registrability/non-infringement.

## Bottom line

| Candidate | Taiwan exact mark result | Software/product collision | Preliminary disposition |
|---|---|---|---|
| **OTTIE** | One exact active Taiwan registration was found: `Ottie`, Class 3, registration no. 01197512, owned by OTTIE INTERNATIONAL CO., LTD., expiring 2036-02-29. No exact Class 9 or 42 match was found in the returned TIPO pool. | **Exact AI software collision:** `Ottie AI - Couples Counseling` is available in Taiwan's Apple App Store. | **Reject as a coding-agent brand.** The exact AI-app collision is close in product form and branding, while the exact cosmetics mark makes the name already occupied in Taiwan even outside software. |
| **OTTI** | No exact `OTTI` Roman-letter mark was found in the returned TIPO pool. Several active marks returned by TIPO's similarity search exist in Classes 9/42, including `iottie`, `OTi`, `Opti`, and `OTTIS` variants. These are candidates for professional review, not findings of legal conflict. | **Multiple exact software uses:** Otti.io is an ideas/team brainstorming app; `otti SaaS` is a travel-management software platform and its owner claims `otti` as a registered trademark; `Otti App` is a Google Play software product. | **Do not adopt without counsel; practically, reject.** It is marginally cleaner in the Taiwan register than OTTIE, but clearly not distinctive or ownable as a global software brand. |

Neither candidate is a clean project/package name. Between the two, `OTTI` has the cleaner *Taiwan exact-register result*, but `OTTIE` and `OTTI` both have exact active software products. A coined name should be screened instead.

## Query method

The official [TIPO Trademark Search System](https://cloud.tipo.gov.tw/S282/S282WV1/) says its text-similarity search covers applied, registered, rejected, and other reference records to surface marks that may create confusion. TIPO separately explains that applicants should use text and device/mark searches before filing: [TIPO trademark-search guidance](https://www.tipo.gov.tw/tw/tipo1/834-1255.html).

I queried the public backend used by that official interface:

1. `POST https://cloud.tipo.gov.tw/S282/S282BV1/api/search/wordSearch`
2. Request body: `{"tmarkDraft":"OTTIE","records":[]}` and separately `{"tmarkDraft":"OTTI","records":[]}`
3. The endpoint returned 756 candidate records for OTTIE and 664 for OTTI.
4. I passed all returned document IDs to `POST /api/result/list`.
5. I normalized `tmark_name` case and checked:
   - exact `OTTIE` / `OTTI` text;
   - records whose class field contained `009` or `042`;
   - active-looking registrations (`itemtype: AB` and a future deadline) versus invalid/expired records.

The official system states new electronic applications may first appear after roughly four days and paper applications after roughly ten days, with longer reconciliation periods. Its displayed data-update date was 2026-08-02, so this screen cannot rule out newer, unpublished, pending, device-only, Chinese-transliteration, common-law, company-name, or overseas rights.

## Exact Taiwan results

### OTTIE

One exact case-insensitive text match appeared:

| Mark | Class | Application | Registration | Owner | Registration notice | Deadline | Dataset status |
|---|---:|---|---|---|---|---|---|
| Ottie | 003 | 094020382 | 01197512 | 歐堤國際股份有限公司 / OTTIE INTERNATIONAL CO., LTD. | 2006-03-01 | 2036-02-29 | `AB` (registered/active pool) |

This is the established Korean cosmetics business. Its company profile identifies `OTTIE INTERNATIONAL`, cosmetics as its main product, and establishment in 2006: [Korea SMEs and Startups Agency company profile](https://ottie.gobizkorea.com/mini/site/companyProfile.do).

No exact `OTTIE` text match in Class 9 or 42 appeared in this query result.

### OTTI

No case-insensitive exact `OTTI` text match appeared in the 664-record query pool. This is a useful negative preliminary result, but it is not a clearance conclusion.

## Similar Roman-letter candidates in software classes

TIPO's text search returned, among others, these active-looking candidates. The table records database output only; it does **not** assert that a court or TIPO examiner would find the marks legally confusing.

| Query | Returned mark | Class | Application | Registration | Deadline | Owner |
|---|---|---:|---|---|---|---|
| OTTIE | iottie | 009 | 105043537 | 01834507 | 2027-04-15 | 劉峻瑋 |
| OTTIE / OTTI | gotti | 009 | 099024313 | 01441332 | 2030-11-30 | GOETTI SWITZERLAND GMBH |
| OTTIE | 歐提斯OTTIS | 009 | 109010704 | 02100134 | 2030-11-15 | 朱煥文 |
| OTTIE | 歐迪斯OTTIS | 009 | 109014472 | 02100137 | 2030-11-15 | 朱煥文 |
| OTTI | OTi 及圖 | 009 | 092061953 | 01110974 | 2034-07-15 | OURS TECHNOLOGY INC. |
| OTTI | Oti 及圖 | 009 | 097040925 | 01362025 | 2029-05-15 | OURS TECHNOLOGY INC. |
| OTTI | Opti | 042 | 080057772 | 00564016 | 2030-08-31 | OPTI GROUP GMBH |

Classes 9 and 42 are the relevant starting point because TIPO materials list downloadable computer software/programs/mobile applications in Class 9 and SaaS/PaaS/software engineering/AI consulting in Class 42: [TIPO Class 9 and 42 example](https://www.tipo.gov.tw/tw/tipo1/424-80944.html). TIPO also notes that Class 9 computer software and Class 42 software/programming services can be closely related for similarity analysis: [TIPO classification presentation](https://www.tipo.gov.tw/wSite/public/Attachment/007/f1744687679274.pdf).

## Exact software/product collisions

### OTTIE

- [Ottie AI - Couples Counseling on Taiwan's App Store](https://apps.apple.com/tw/app/ottie-ai-couples-counseling/id6754530245) is an active iPhone AI product by Vova Ventures LLC. The listing calls the product `Ottie AI`, describes an AI mediator/chat experience, and shows releases from 2025 through 2026.
- This is much more relevant to a friendly coding-agent brand than the Class 3 cosmetics registration: same exact core name, AI-assistant positioning, app/software format, and availability in Taiwan.

### OTTI

- [Otti.io](https://otti.io/) is an exact-name software product for capturing ideas, sharing feedback, and team brainstorming.
- [otti SaaS](https://otti.omeron.com/) is an exact-name travel-management software platform with task, booking, notification, API, and extension functions. Its footer states that Omeron Group treats `otti` and associated app names as registered trademarks, but it does not identify the registration jurisdiction or number; this claim was **not independently confirmed in an official registry in this Taiwan sub-screen**.
- [Otti App on Google Play](https://play.google.com/store/apps/details?id=otti.user.laundry) is an exact-name app with 5,000+ downloads in the retrieved listing. Taiwan availability was not independently confirmed.
- [Otti SaaS Assistant on the Chrome Web Store](https://chromewebstore.google.com/detail/otti-saas-assistant/amkehjeibmaoocigmpmdfffgmjndgpah) is an exact-name browser software extension from Omeron Technologies.

These uses do not automatically establish enforceable Taiwan trademark rights. They do show that `OTTI` is already crowded in the exact software/product namespace and would be difficult to search for, own, and distinguish.

## Source-depth inventory

| Source | Read level | Use | Blocker/limitation |
|---|---|---|---|
| TIPO Trademark Search System UI and public API | ✅ First-party, direct query | Exact/similar Taiwan case data and update date | Search result is informational; not a legal status certificate or full legal similarity analysis. |
| TIPO trademark-search guidance | ✅ First-party page | Confirms recommended search scope and purpose | General guidance, not candidate-specific. |
| TIPO Class 9/42 materials | ✅ First-party pages/searchable official material | Confirms software-related class relevance | Exact goods/service drafting still requires a filing-specific list. |
| Taiwan Apple App Store, Ottie AI | ✅ First-party marketplace listing | Confirms exact AI app available to Taiwan users | App Store presence does not prove a Taiwan trademark registration. |
| Otti.io and otti SaaS official product sites | ✅ First-party product pages | Confirms exact software uses | Omeron's trademark claim lacks registration metadata on the page. |
| Google Play / Chrome Web Store listings | ✅ First-party marketplaces | Confirms exact software product names | Taiwan distribution for Otti App was not proven. |

## Limitations and next legal step

This screen did not cover logo/device marks by image, Chinese transliterations, company/trade-name registries exhaustively, domain disputes, unregistered reputation, every Nice class, or full international trademark families. It also did not determine likelihood of confusion under Taiwan law.

If either name remains under consideration, a Taiwan trademark professional should run and interpret:

1. exact and fuzzy Roman-letter searches for `OTTIE`, `OTTI`, `OTI`, `OTTIS`, `IOTTIE`, and phonetic/transliteration variants;
2. Classes 9 and 42 with the intended downloadable CLI, hosted agent, SaaS, AI, and software-development goods/services wording;
3. applicant/owner and international-priority checks for the exact software products above;
4. company-name and market-use searches; and
5. a global clearance search for the intended launch markets.

For product naming today, the evidence supports dropping both names and screening a more distinctive coined alternative.
