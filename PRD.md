# AML False Positive Discounter — Spec

**Status:** Build-ready
**Version:** v2 (verified sources, corrected URLs, parser details)
**Date:** 2026-04-14

---

## The Problem

ZAR and partners like RAIN and WhaloPay do KYC + sanctions screening on users. Roughly 10-15% of users (~100-150/day at current volume) hit common-name false positives (Muhammad, Khan, Singh, etc.). Partners reject these but give us no detail on which match triggered. To clear these users, someone has to manually compare the user to every possible match on every screened list.

We build a tool that automates that comparison using Claude inference and produces a clean audit-ready report. The compliance officer at any partner gets a defensible document explaining why each match is a false positive (or isn't).

## The Tool

A single self-contained application. You feed in a user's details. It:
1. Matches the user against public sanctions and PEP lists (~15 authoritative sources)
2. For every potential match, asks Claude to compare the user to the matched record
3. Returns a structured report saying "cleared," "likely match," or "needs human review," with reasoning per match
4. Logs everything to an audit database

It does not integrate with anyone's API. You paste in user details, get a report, paste or attach the report wherever you need it. Usable with any partner.

---

## Data Sources

All free, all direct from authoritative publishers. We use OpenSanctions' GitHub repo (`opensanctions/opensanctions/datasets/`, MIT-licensed) as a reference for source URLs, XML schemas, parsing quirks, and edge cases. We never redistribute their processed data.

Every URL below has been verified live (2026-04-14). HTTP status codes, file sizes, and record counts are from actual requests.

### Core Sanctions (Tier 1)

These are the lists every US/international fintech partner screens against. Non-negotiable.

| # | List | URL | Format | Size | Individuals | Headers Required | Refresh |
|---|------|-----|--------|------|-------------|-----------------|---------|
| 1 | OFAC SDN | `https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML` | XML | 117 MB | ~7,400 | `User-Agent` (any value; 403 without it) | Daily |
| 2 | OFAC Consolidated (Non-SDN) | `https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/CONS_ADVANCED.XML` | XML | 4 MB | ~100 | `User-Agent` | Daily |
| 3 | UN Security Council | `https://scsanctions.un.org/resources/xml/en/consolidated.xml` | XML | 2 MB | 733 | None | Daily |
| 4 | EU Consolidated (FSF) | `https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw` | XML | 23 MB | ~4,000 | None (token in URL is static/public) | Daily |
| 5 | UK Sanctions List | `https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml` | XML | 20 MB | ~3,800 | None | Daily |

**Notes:**
- OFAC SLS redirects to a pre-signed S3 URL. HTTP client must follow redirects.
- OFAC SDN is 117 MB. Must use streaming XML parser (SAX/iterparse), NOT DOM loading. Budget 30-60s download on slow connections.
- UN returns `Content-Type: application/octet-stream` despite being XML. Parse by content, not header.
- EU token `dG9rZW4tMjAxNw` is base64 for "token-2017". It's a public static value, not authentication.
- UK is the fastest source (CDN, <1s download).

### Extended Sanctions (Tier 2)

Adds credibility. Same sanctioned persons largely overlap with Tier 1, but having independent source coverage strengthens the report.

| # | List | URL | Format | Size | Individuals | Notes |
|---|------|-----|--------|------|-------------|-------|
| 6 | Canada (DFATD/SEMA) | `https://www.international.gc.ca/world-monde/assets/office_docs/international_relations-relations_internationales/sanctions/sema-lmes.xml` | XML | ~1 MB | ~2,800 | Simple XML, `<record>` elements. Aliases embedded with language prefixes ("Russian: Олег..."). |
| 7 | Australia (DFAT) | `https://www.dfat.gov.au/sites/default/files/Australian_Sanctions_Consolidated_List.xlsx` | XLSX | ~2 MB | ~2,000 | Requires browser `User-Agent` header. **Geo-blocked from some non-AU IPs.** Entries grouped by reference number; aliases are sub-rows (101, 101a, 101b). Date formats are extremely messy. |
| 8 | Switzerland (SECO) | `https://www.sesam.search.admin.ch/sesam-search-web/pages/downloadXmlGesamtliste.xhtml?lang=en&action=downloadXmlGesamtlisteAction` | XML | ~5 MB | ~3,000 | Complex XML with `<target>/<individual>` structure. Name parts have explicit types (given-name, family-name, father-name, tribal-name). Skip `<entity>` and `<object>` children. |

**Notes:**
- Canada original OSFI URL (`osfi-bsif.gc.ca/Eng/Docs/...`) returns 404 (site restructured). Use the DFATD/SEMA URL above (verified working).
- Australia may need a fallback strategy (skip if geo-blocked, or route through proxy). Not critical since the same individuals appear on UN/OFAC/EU lists.
- Switzerland old URL (`seco.admin.ch/dam/seco/...`) returns 404 (site restructured). Use the SESAM URL above (verified via OpenSanctions crawler).

### PEPs (Tier 2)

Partners screen against PEP lists. We need coverage of current and recent political figures globally, with good depth on our key markets.

| # | Source | Method | Records | Notes |
|---|--------|--------|---------|-------|
| 9 | Wikidata Global PEPs | SPARQL API at `https://query.wikidata.org/sparql` | ~288,000 distinct PEPs globally; ~850 Pakistani PEPs | Must query per-country (global query with labels times out at 60s). 9 PEP position types: head of state, head of government, minister, MP, legislator, judge, governor, ambassador, senator. Weekly refresh sufficient. |
| 10 | US Congress | `https://raw.githubusercontent.com/unitedstates/congress-legislators/main/legislators-current.yaml` | 538 current legislators | CC0 license. Also: `legislators-historical.yaml` (~12,000 historical). Clean YAML with bioguide IDs, names, DOBs, terms. |
| 11 | UK Parliament | `https://members-api.parliament.uk/api/Members/Search?skip=0&take=20` | 5,227 current + historical | JSON REST API, paginated (`skip`/`take`). No auth. Includes Lords and Commons. |
| 12 | EU Parliament | `https://www.europarl.europa.eu/meps/en/full-list/xml` | 718 current MEPs | Tiny XML, single line. Name + country + political group per MEP. Current members only. |

**Wikidata SPARQL query (tested, working):**
```sparql
SELECT DISTINCT
  ?person ?personLabel ?positionLabel ?pepTypeLabel ?startDate ?endDate
WHERE {
  ?person wdt:P31 wd:Q5 .
  ?person p:P39 ?stmt .
  ?stmt ps:P39 ?position .
  ?position wdt:P279 ?pepType .
  VALUES ?pepType {
    wd:Q48352 wd:Q2285706 wd:Q83307 wd:Q486839
    wd:Q4175034 wd:Q16533 wd:Q132050 wd:Q121998 wd:Q15686806
  }
  { ?position wdt:P1001 ?country . }
  UNION
  { ?position wdt:P17 ?country . }
  FILTER (?country = wd:Q843)
  OPTIONAL { ?stmt pq:P580 ?startDate . }
  OPTIONAL { ?stmt pq:P582 ?endDate . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" . }
}
ORDER BY ?personLabel
```
Replace `wd:Q843` (Pakistan) with target country QID. Key QIDs: US=Q30, UK=Q145, UAE=Q878, India=Q668, SA=Q851, Nigeria=Q1033, Bangladesh=Q902.

**Pakistani PEP breakdown from Wikidata:**
- 601 Members of Parliament
- 200 Senators
- 142 Governors
- 63 Ministers
- 30 Heads of Government
- 15 Heads of State
- 1 Judge (gap: judiciary is extremely underrepresented)

### Most Wanted (Tier 3)

Lower priority. Adds coverage for fugitives and terrorism suspects beyond sanctions lists.

| # | Source | Method | Records | Notes |
|---|--------|--------|---------|-------|
| 13 | FBI Most Wanted | `https://api.fbi.gov/wanted/v1/list?pageSize=20&page=1` | 1,151 | Open JSON API, paginated. No auth. Rich data (images, descriptions, rewards). |
| 14 | Interpol Red Notices | `https://ws-public.interpol.int/notices/v1/red` | ~6,000 | **Currently blocked by Akamai CDN (403).** Requires browser-like headers (User-Agent + Referer: `https://www.interpol.int/` + Origin: `https://www.interpol.int`). OpenSanctions uses complex faceted querying (by nationality, gender, age) with 0.1s delays to enumerate all results past the 160-per-page limit. Defer to Phase 2 unless headers alone fix the 403. |

### Source Count Summary

**MVP (11 working sources):** OFAC SDN, OFAC Consolidated, UN, EU, UK, Canada, Switzerland, Wikidata PEPs, US Congress, UK Parliament, EU Parliament, FBI Most Wanted.

**Deferred (3 sources):** Australia (geo-blocked, retry with proxy), Interpol (403, needs investigation), Japan METI (PDF/XLSX, low priority).

Report language: *"Screened against 11 authoritative sources: OFAC (SDN + Consolidated Non-SDN), UN Security Council, EU Financial Sanctions, UK Sanctions List, Canada SEMA, Switzerland SECO, Wikidata Global PEPs (288K politicians across 194 countries), US Congress, UK Parliament, EU Parliament, FBI Most Wanted."*

---

## Architecture

One box. Python. No microservices, no message queues, no search cluster.

```
┌──────────────────────────────────────────────┐
│  Single Docker container                     │
│                                              │
│  ┌─────────────────┐                         │
│  │  Fetcher        │  cron daily,            │
│  │  (per source)   │  pulls XML/JSON/XLSX,   │
│  │                 │  parses into unified     │
│  │                 │  schema, streams to DB   │
│  └────────┬────────┘                         │
│           ▼                                  │
│  ┌─────────────────┐                         │
│  │  SQLite         │  ~25K-30K individuals   │
│  │  + FTS5 index   │  (all lists combined)   │
│  └────────┬────────┘                         │
│           ▼                                  │
│  ┌─────────────────┐                         │
│  │  Matcher        │  FTS5 + rapidfuzz       │
│  │                 │  + phonetic + ICU        │
│  │                 │  returns top N candidates│
│  └────────┬────────┘                         │
│           ▼                                  │
│  ┌─────────────────┐                         │
│  │  Discounter     │  one Claude call per    │
│  │  (Claude Sonnet)│  ambiguous candidate    │
│  └────────┬────────┘                         │
│           ▼                                  │
│  ┌─────────────────┐                         │
│  │  Report gen     │  HTML, PDF, JSON        │
│  └────────┬────────┘                         │
│           ▼                                  │
│  ┌─────────────────┐                         │
│  │  Audit log      │  SQLite, 7-year retain  │
│  └─────────────────┘                         │
└──────────────────────────────────────────────┘
```

### Unified Entity Schema

```python
@dataclass
class ListEntry:
    id: str                   # source-prefixed: "ofac-12345", "un-QDi.137", "wd-Q1234"
    source: str               # "ofac_sdn", "un_consolidated", "eu_fsf", "wikidata_peps", etc.
    list_name: str            # "OFAC SDN List", "UN Security Council 1267/1989/2253", etc.
    names: list[str]          # primary name + all aliases + transliteration variants
    alias_quality: list[str]  # per-name: "strong", "weak", "unknown" (OFAC/UN classify this)
    dob: list[str]            # ISO dates, year-only ("1975"), or ranges ("1963-1968")
    dob_approximate: bool     # true if any DOB is circa/range
    pob: list[str]            # place of birth (city, country)
    nationality: list[str]    # ISO country codes where available
    gender: str | None        # "male", "female", None
    identifiers: list[dict]   # [{type: "passport", value: "AB1234567", country: "PK"}, ...]
    addresses: list[str]      # full address strings
    designation: str | None   # "Taliban deputy minister", "Member of National Assembly"
    listing_reason: str | None
    listed_on: str | None     # ISO date
    programs: list[str]       # ["SDGT", "NPWMD", "RUSSIA-EO14024"]
    source_url: str           # link to authoritative source for audit
    raw: dict                 # original parsed record
```

Individuals only. Entity/vessel/aircraft records filtered at ingest.

### Record Counts Per Source (individuals, verified)

| Source | Individuals | Overlap notes |
|--------|-------------|---------------|
| OFAC SDN | ~7,400 | Superset includes UN-transposed designations |
| OFAC Consolidated | ~100 | Small; SSI/CAPTA/NS-CMIC programs |
| UN SC | ~730 | Subset of OFAC (US transposes all UN designations) |
| EU FSF | ~4,000 | ~80% overlap with OFAC + UN |
| UK Sanctions | ~3,800 | ~80% overlap with EU; some UK-autonomous designations |
| Canada SEMA | ~2,800 | High overlap with UN/OFAC |
| Switzerland SECO | ~3,000 | High overlap with EU |
| Wikidata PEPs | ~288,000 | No overlap with sanctions (PEPs != sanctioned) |
| US Congress | 538 | Subset of Wikidata PEPs |
| UK Parliament | 5,227 | Mostly in Wikidata; historical adds unique |
| EU Parliament | 718 | Subset of Wikidata PEPs |
| FBI Most Wanted | 1,151 | Minimal overlap with sanctions lists |
| **De-duplicated estimate** | **~25,000-30,000 unique individuals** | After cross-list dedup |

---

## Parser Details (Per Source)

### OFAC SDN Advanced XML (the hard one)

The OFAC Advanced XML is the most complex format. Data for one individual is scattered across **four separate top-level XML sections** joined by foreign keys:

1. **`DistinctParties/DistinctParty/Profile`** — the person (names, DOB, gender, nationality via Features)
2. **`IDRegDocuments/IDRegDocument`** — passports, national IDs (joined by `IdentityID`)
3. **`SanctionsEntries/SanctionsEntry`** — programs, legal basis, listing date (joined by `ProfileID`)
4. **`Locations/Location`** — addresses, countries (joined by `LocationID`)

**Parser must:**
1. Pre-index all four sections into dicts by their join keys (one pass through the XML)
2. Strip XML namespace at load time (as OpenSanctions does)
3. Use `iterparse` for streaming — 117MB cannot be DOM-loaded

**Name structure:** Alias → DocumentedName → DocumentedNamePart → NamePartValue, where NamePartGroupID joins to Identity/NamePartGroups to determine name part type (Last=1520, First=1521, Middle=1522, Patronymic=91708). Primary name has `Alias@Primary="true"`. Weak aliases have `@LowQuality="true"`.

**Date structure:** `DatePeriod/Start|End/From|To/Year,Month,Day`. If all four corners are identical → exact date. If Jan-1 to Dec-31 of same year → year-only. Otherwise → true range.

**Feature types we care about (by FeatureTypeID):**
| ID | Field |
|----|-------|
| 8 | Date of birth |
| 9 | Place of birth |
| 10 | Nationality |
| 11 | Citizenship |
| 25 | Address (residential) |
| 224 | Gender (91526=Male, 91527=Female) |

**Identity documents:** 6,230 docs for individuals. Top types: Passport (2,540), National ID (1,288), Tax ID (542), Cedula (506), CURP (540).

**Lines of code:** OpenSanctions' parser is 828 lines. For individuals-only, **~200 lines** after stripping vessel/aircraft/crypto/relationship handling and replacing FtM entity model with plain dicts.

### UN Consolidated XML (straightforward)

Simple flat XML. `<INDIVIDUAL>` elements with direct child fields: `FIRST_NAME`, `SECOND_NAME`, `THIRD_NAME`, `FOURTH_NAME`, `GENDER`, `UN_LIST_TYPE`, `REFERENCE_NUMBER`, `LISTED_ON`, `COMMENTS1`, `NAME_ORIGINAL_SCRIPT`. Nested repeatable elements: `INDIVIDUAL_ALIAS` (with `QUALITY`: Good/Low), `INDIVIDUAL_DATE_OF_BIRTH` (with `TYPE_OF_DATE`: EXACT/APPROXIMATELY/BETWEEN), `INDIVIDUAL_PLACE_OF_BIRTH`, `INDIVIDUAL_DOCUMENT`, `INDIVIDUAL_ADDRESS`.

~80 lines of parser code.

### EU FSF XML (moderate)

`<sanctionEntity>` elements with `<subjectType code="person">` for individuals. Names via `<nameAlias>` elements (firstName, middleName, lastName, wholeName, gender, function). Aliases in all 24 EU official languages (5+ nameAlias per person on average — transliteration variants, not distinct identities). `<birthdate>` with circa flag, `<citizenship>`, `<address>`, `<identification>` (passport, regnumber, fiscalcode, etc.). Programme code on nested `<regulation>` element.

~150 lines of parser code.

### UK Sanctions XML (moderate)

`<Designation>` elements. Entity type from `<IndividualEntityShip>` tag. Names use Name1-Name6 scheme (Name1=given, Name6=family — counterintuitive). Aliases via `<NameType>` (Primary Name, Alias, Primary Name Variation). `<AliasStrength>` only on UN-originated entries. Download URL is dynamic: scrape `gov.uk/government/publications/the-uk-sanctions-list` for current XML link, or use the stable `sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml` (verified working directly).

~120 lines of parser code.

### Canada SEMA XML (simple)

`<record>` elements. Determine entity type by presence of fields (GivenName/LastName/DateOfBirth present → person). Aliases embedded in name fields with language prefixes ("Russian: Олег..."). Country field is bilingual ("Russia / Russie"). Some corrupt dates ("31801", "31948").

~60 lines of parser code.

### Switzerland SECO XML (moderate-complex)

`<target>` elements with `<individual>`, `<entity>`, `<object>` children. Filter to `<individual>` only. Name parts have explicit types: given-name, family-name, father-name, maiden-name, tribal-name. Multiple spelling variants per name. Delisted entries (modification-type="de-listed") must be skipped. OpenSanctions uses GPT for `<other-information>` extraction — we skip this entirely.

~150 lines of parser code.

### Australia DFAT XLSX (moderate)

XLSX with reference-number grouping (primary entry "101", aliases "101a", "101b"). Entity type from "type" column. Date formats are extremely messy — handles "between 1963 and 1968", "1972-08-10 or 1972-08-11", corrupt formats like "196719611973", "2/05/196". Needs browser User-Agent. May be geo-blocked.

~100 lines of parser code (using openpyxl).

### Wikidata PEPs (SPARQL)

Per-country SPARQL queries (global query times out). Parse JSON bindings into ListEntry. Extract Wikidata QID as entity ID. Position title as designation. Country from query parameter. No DOB or identifier data from Wikidata PEPs (gap: only name + position + country + dates of service).

~80 lines of code (query builder + response parser).

### US Congress, UK Parliament, EU Parliament (simple)

YAML/JSON/XML with clean schemas. 20-40 lines each.

### FBI Most Wanted (simple)

JSON API with pagination. Rich data per record. 30 lines.

### Interpol Red Notices (complex, deferred)

Requires browser-like headers and complex faceted querying strategy to enumerate past 160-per-page limit. OpenSanctions' crawler is 217 lines with nationality × gender × age decomposition. Defer to Phase 2.

### Total parser code estimate

| Source | Lines |
|--------|-------|
| OFAC SDN + Consolidated | ~200 |
| UN Consolidated | ~80 |
| EU FSF | ~150 |
| UK Sanctions | ~120 |
| Canada SEMA | ~60 |
| Switzerland SECO | ~150 |
| Australia DFAT | ~100 |
| Wikidata PEPs | ~80 |
| US Congress | ~30 |
| UK Parliament | ~30 |
| EU Parliament | ~20 |
| FBI Most Wanted | ~30 |
| Shared utilities (dates, names, transliteration) | ~200 |
| **Total** | **~1,250 lines** |

---

## Matching Engine

### Stage 1: Name Candidate Generation

SQLite FTS5 full-text search with a secondary rapidfuzz re-rank. Returns top 20 candidates per query.

**Index structure:**
```sql
CREATE VIRTUAL TABLE names_fts USING fts5(
    entity_id,
    name_normalized,   -- lowercase, ASCII-folded, diacritics stripped
    name_phonetic,     -- Double Metaphone encoding
    name_original,     -- preserved for display
    source,
    tokenize='porter unicode61 remove_diacritics 2'
);
```

**Query strategy (multi-pass, union results):**
1. Exact FTS match on normalized name
2. Phonetic match on Metaphone encoding (catches "Mohammed" ↔ "Muhammad" ↔ "Mehmet")
3. Token-sorted match (catches "John Smith" ↔ "Smith, John")
4. Re-rank all candidates using `rapidfuzz.fuzz.token_sort_ratio` + `rapidfuzz.distance.JaroWinkler`
5. Return top 20 by composite score

**Transliteration:** Use `icu` (PyICU) for Arabic/Cyrillic/CJK → Latin before indexing and querying. Preserve originals for display.

**Dependencies:** `rapidfuzz`, `PyICU` (or `icu-tokenizer`), `jellyfish` (for Double Metaphone), `sqlite3` (stdlib).

~200 lines total.

### Stage 2: Cross-List Deduplication

The same sanctioned person (e.g., Hafiz Saeed) appears on OFAC, UN, EU, and UK lists simultaneously. Without dedup, that's 4 match records for 1 actual person, wasting 4x the Claude tokens.

**Before any scoring or LLM calls, group raw candidates into unique persons:**

```python
def dedup_candidates(candidates: list[ListEntry]) -> list[DeduplicatedMatch]:
    """
    Group candidates from different lists that refer to the same
    real-world person. Returns one DeduplicatedMatch per unique person,
    with all source lists attached.
    """
    groups = []
    for candidate in candidates:
        merged = False
        for group in groups:
            if is_same_person(group.representative, candidate):
                group.add_source(candidate)
                merged = True
                break
        if not merged:
            groups.append(DeduplicatedMatch(candidate))
    return groups

def is_same_person(a: ListEntry, b: ListEntry) -> bool:
    # Exact identifier match (CNIC, passport) → definitely same
    if shared_identifier(a.identifiers, b.identifiers):
        return True
    # High name similarity + matching DOB → same
    name_sim = max(rapidfuzz.fuzz.token_sort_ratio(na, nb)
                   for na in a.names for nb in b.names) / 100
    dob_match = bool(set(a.dob) & set(b.dob))
    nat_match = bool(set(a.nationality) & set(b.nationality))
    if name_sim > 0.90 and dob_match:
        return True
    if name_sim > 0.95 and nat_match:
        return True
    return False

@dataclass
class DeduplicatedMatch:
    representative: ListEntry  # richest record (most fields populated)
    all_sources: list[dict]    # [{source, list_name, source_url, listed_on, programs}]
    all_names: list[str]       # union of all name variants across sources
    all_identifiers: list[dict]
```

**Effect:** 80 raw candidates across 11 lists typically dedup to ~25-35 unique persons. The remaining unique persons go through pre-score and LLM analysis. The report shows each unique person once, with "Appears on: OFAC SDN, UN 1267, EU, UK HMT" underneath.

### Stage 3: Deterministic Pre-Score (fast filter)

Apply cheap checks to auto-clear obvious non-matches before any Claude calls:

```python
def pre_score(user, match: DeduplicatedMatch) -> str:
    candidate = match.representative

    # Gender conflict → auto-clear
    if user.gender and candidate.gender and user.gender != candidate.gender:
        return "auto_clear"

    # Exact CNIC/passport match → auto-flag (true positive)
    if shared_identifier(user.identifiers, match.all_identifiers):
        return "auto_flag"

    # DOB conflict >10 years → auto-clear
    if user_year and candidate_year and abs(user_year - candidate_year) > 10:
        return "auto_clear"

    # Everything else → needs LLM judgment
    return "send_to_llm"
```

Eliminates ~50-70% of unique persons. Typical flow for 80 raw candidates:
- 80 raw → 30 unique persons (after dedup)
- 30 unique → 18 auto-cleared (gender/DOB/ID conflicts)
- 30 unique → 0-1 auto-flagged (exact ID match)
- **~12 sent to Claude**

### Stage 4: Claude Discounting (single batched call)

All remaining candidates go to Claude in **one prompt**. The model sees the user plus every ambiguous match simultaneously, which lets it:
- Recognize when multiple matches are variants of the same UN-listed individual
- Apply consistent reasoning across the full set
- Produce one coherent report

**Token budget for the batched call:**
- System prompt: ~500 tokens
- User record: ~200 tokens
- Per match: ~300 tokens (name, aliases, DOB, nationality, identifiers, designation, listing reason, source lists)
- 12 matches × 300 = 3,600 tokens input
- Output (12 decisions with reasoning): ~3,600 tokens
- **Total: ~8,000 tokens per screening**

Even worst case (40 matches surviving to LLM): ~500 + 200 + 12,000 input + 12,000 output = ~25,000 tokens. Well within Sonnet's context.

**Prompt:**

```
You are an AML compliance analyst. A customer is being screened for
sanctions and PEP matches. Below are the customer's details and a list
of potential matches from global sanctions and PEP databases.

For EACH match, decide whether it could plausibly be the same person
as the customer.

RULES:
- Default assumption: SAME PERSON, unless you find explicit contradictions.
- Name variations (spelling, transliteration, missing middle name) are
  EXPECTED and common. They do NOT indicate different people.
- Missing fields are NORMAL. Absence of data is NOT evidence of difference.
- Flag as DIFFERENT only with EXPLICIT contradictions:
  - Different dates of birth (>5 years apart)
  - Different genders
  - Conflicting national IDs (CNIC, passport number)
  - Clearly incompatible nationalities (no plausible dual citizenship)
  - Fundamentally different biographical profiles

CUSTOMER:
{user_json}

POTENTIAL MATCHES ({n} unique persons):

MATCH 1 (appears on: {source_list_1}, {source_list_2}):
{match_1_json}

MATCH 2 (appears on: {source_list_3}):
{match_2_json}

[... all N matches ...]

For each match, respond with a JSON array:
[
  {
    "match_number": 1,
    "decision": "CLEARED" | "LIKELY_MATCH" | "ESCALATE",
    "confidence": 0.0-1.0,
    "contradictions": [
      {
        "field": "dob",
        "user_value": "1994-01-20",
        "match_value": "circa 1975",
        "interpretation": "19-year gap makes same-person unlikely"
      }
    ],
    "supporting_similarities": [
      {
        "field": "nationality",
        "user_value": "Pakistan",
        "match_value": "Pakistan",
        "interpretation": "Same nationality, consistent but not dispositive"
      }
    ],
    "reasoning": "One-paragraph plain-English summary for a compliance officer."
  },
  ...
]
```

**Model:** Claude Sonnet 4.6 (`claude-sonnet-4-6`). Strong structured reasoning at $3/$15 per MTok. The task (compare biographical records, find contradictions) is well within Sonnet's capability. Opus is overkill. Haiku works for obvious cases but may miss nuance on close calls (same nationality, similar age, partial name overlap).

**Cost per screening (batched):**
- ~8,000 tokens typical (12 matches) = ~$0.03
- ~25,000 tokens worst case (40 matches) = ~$0.09
- 150 flagged users/day × $0.05 average = ~$7.50/day = **~$225/month**

**Prompt caching:** System prompt (~500 tokens) is identical across all calls. With Anthropic prompt caching, that portion costs 0.1x on cache hits. Saves ~10% on input tokens.

**Accuracy:** The Federal Reserve study (FEDS 2025-092) found 92% false positive reduction with GPT-4o-class models on this exact task. The key to accuracy isn't model size, it's prompt design (contradiction-detection framing) + data richness (full biographical records, not just name + score). Both are handled here.

---

## Report Output

One XLSX file per screening. The compliance officer opens it, sees the user against all matches, and understands immediately what was found and why each match was discounted (or wasn't).

### Output formats (priority order)

1. **XLSX** (primary) — one file per screening, the working document
2. **JSON** (for automation) — same data, machine-readable
3. **PDF** (optional, on demand) — generated from web UI for archiving individual cases

### XLSX Structure

**Sheet 1: "Summary"**

Single-column key-value layout. The compliance officer glances at this to get the verdict.

| Field | Value |
|-------|-------|
| **RESULT** | **CLEAR** |
| Subject | Muhammad Naeem Ahmed |
| Date of Birth | 1994-01-20 |
| Nationality | Pakistan |
| CNIC | 35202-5030579-1 |
| Gender | Male |
| Screened | 2026-04-14 09:23:15 UTC |
| Sources | OFAC SDN, OFAC Consolidated, UN, EU, UK, Canada, Switzerland, Wikidata PEPs, US Congress, UK Parliament, EU Parliament, FBI (11 sources) |
| Data Freshness | All lists updated within last 24 hours |
| Raw Candidates | 80 across 7 lists |
| Unique Persons | 28 (after cross-list dedup) |
| Auto-Cleared | 16 (gender/DOB/ID conflicts) |
| AI Analyzed | 12 |
| Flagged | 0 |
| Escalated | 0 |
| Report ID | SCR-20260414-000347 |
| Model | claude-sonnet-4-6 |
| Processing Time | 3.1 seconds |
| Note | This report is an analytical aid. Final determination rests with the reviewing compliance team. |

**Sheet 2: "Matches" — the core of the report**

One row per unique person (after dedup). This is what the compliance officer actually reads.

| # | Decision | Confidence | Cleared By | Matched Person | Aliases | DOB (match) | Nationality (match) | Gender (match) | Designation | Source Lists | Identifiers (match) | Key Contradiction | AI Reasoning |
|---|----------|------------|-----------|----------------|---------|-------------|--------------------|----|-------------|-------------|---------------------|-------------------|--------------|
| 1 | CLEARED | — | Rule: gender | Fatima Naeem | — | 1980 | PK | F | — | NACTA | — | Gender: user M, match F | Auto-cleared: gender conflict |
| 2 | CLEARED | — | Rule: DOB | Muhammad Naeem | Mullah Naeem Barich | ~1960 | AF | M | Taliban commander | UN, OFAC, EU, UK | — | DOB: 1994 vs ~1960 (34yr gap) | Auto-cleared: >10yr DOB gap |
| 3 | CLEARED | 0.96 | AI | Muhammad Naeem | Haji Naeem | ~1975 | AF | M | Taliban deputy minister | UN (QDi.137), OFAC, EU | — | DOB: 19yr gap; nationality: PK vs AF; profile: civilian vs military | 19-year age difference, different nationality (Pakistani vs Afghan), completely different biographical profile. No shared identifiers. False positive. |
| 4 | CLEARED | 0.93 | AI | Muhammad Naeem Akhtar | — | unknown | PK | M | Proscribed (4th Schedule) | NACTA | CNIC: 42101-XXXXXXX | CNIC prefix mismatch: 35202 vs 42101 | Name is partial overlap only. Different CNIC prefixes (different registration districts). Despite same nationality, insufficient evidence of same person. |
| 5 | CLEARED | 0.88 | AI | M. Naeem | — | 1967 | PK | M | Member National Assembly (2008-2013) | Wikidata PEPs | — | DOB: 1994 vs 1967 (27yr gap) | PEP match. 27-year age difference. Former MNA now out of office. Different generation. |
| ... | | | | | | | | | | | | | |
| 28 | CLEARED | 0.91 | AI | Mohammad Naim | — | ~1978 | AF | M | Taliban district governor | UN (TAi.XXX) | — | DOB: 16yr gap; nationality: PK vs AF | Different nationality and generation. Taliban district governor in Helmand, Afghanistan. No connection to user's profile. |

Column widths auto-sized. Decision column uses conditional formatting: CLEARED = green fill, LIKELY_MATCH = red fill, ESCALATE = yellow fill.

The compliance officer:
1. Opens the file
2. Looks at Sheet 1: sees CLEAR, 0 flagged
3. Scans Sheet 2: sees all rows are green, reads the Key Contradiction column to confirm each makes sense
4. Done. Approves the user. Total time: 30 seconds.

For a FLAG or ESCALATE result, they'd see red/yellow rows, read the AI Reasoning column for those specific matches, and make a human judgment call.

**Sheet 3: "Audit" — raw data for regulators**

One row per Claude API call (typically one row for the batched call, but logged separately if retried).

| Field | Value |
|-------|-------|
| Report ID | SCR-20260414-000347 |
| Timestamp | 2026-04-14 09:23:15 UTC |
| Model | claude-sonnet-4-6 |
| Input Tokens | 4,100 |
| Output Tokens | 3,600 |
| Prompt (full) | [full prompt text] |
| Response (full) | [full JSON response] |
| Source Versions | ofac_sdn: sha256:abc123..., un: sha256:def456..., eu_fsf: sha256:789... |

Not for daily use. Exists so an auditor can reconstruct exactly what the AI saw and said, years later.

### XLSX Generation

Use `openpyxl` (already a dependency for Australia DFAT parser). Apply:
- Conditional formatting on Decision column (green/red/yellow fills)
- Auto-filter on all columns in Sheet 2
- Freeze top row
- Auto-width columns
- Bold headers

~50 lines of code. No WeasyPrint or HTML templating needed (removing that dependency).

### Why not Claude Managed Agents

The spec uses the Claude Messages API (direct inference calls), not Managed Agents. Managed Agents is designed for multi-step, tool-heavy, long-running agent sessions. Our use case is a single structured inference call per screening (take 12 match records, return 12 decisions). Messages API is faster (no container startup), cheaper (Batch API gives 50% off, prompt caching gives 90% off system prompt), simpler (one HTTP POST), and keeps AML data out of Anthropic's hosted infrastructure. Managed Agents adds session management, sandboxed containers, and built-in tools (bash, file I/O, web search) that we don't use. If we later build a "compliance investigation" feature where an AI agent researches flagged entities in depth (browsing web, pulling records, drafting SAR narratives), that would be a Managed Agents use case. But v1 screening is not.

---

## Audit Log

SQLite table. Every screening gets a row.

```sql
CREATE TABLE screenings (
    id TEXT PRIMARY KEY,           -- "SCR-20260414-000347"
    created_at TEXT NOT NULL,      -- ISO timestamp
    user_input TEXT NOT NULL,      -- JSON of user details
    source_versions TEXT NOT NULL, -- JSON: {source: sha256_of_file, ...}
    raw_candidates INTEGER,          -- before dedup (e.g. 80)
    unique_persons INTEGER,          -- after dedup (e.g. 28)
    auto_cleared INTEGER,            -- by pre-score (e.g. 16)
    auto_flagged INTEGER,            -- exact ID match (e.g. 0)
    sent_to_llm INTEGER,             -- ambiguous (e.g. 12)
    llm_cleared INTEGER,
    llm_flagged INTEGER,
    llm_escalated INTEGER,
    result TEXT NOT NULL,          -- "CLEAR" | "FLAG" | "ESCALATE"
    matches TEXT NOT NULL,         -- JSON array of all matches with decisions
    llm_calls TEXT NOT NULL,       -- JSON array of {input, output, model, tokens}
    report_json TEXT NOT NULL,     -- full report as JSON
    processing_ms INTEGER
);
```

Retention: 7 years (2,555 days). Cron job purges older records.

An auditor can reconstruct everything: what data was available, what matched, what the AI said, and why.

---

## User Interface

### CLI

```bash
# Screen a single user
aml-screen --name "Muhammad Naeem" --dob 1994-01-20 --nationality PK --cnic 35202-5030579-1

# Screen from JSON file
aml-screen --input user.json --output report.pdf

# Bulk screen (CSV of users)
aml-screen --bulk users.csv --output-dir reports/

# Refresh all list data
aml-screen --refresh

# Show data freshness
aml-screen --status
```

### Web UI (localhost)

Single page. One form. One button.

```
┌──────────────────────────────────────────────────┐
│  AML Discounter                                  │
│                                                  │
│  Full name:      [                            ]  │
│  Date of birth:  [          ]                    │
│  Nationality:    [                          ▾]   │
│  Place of birth: [                            ]  │
│  CNIC / Nat. ID: [                            ]  │
│  Passport:       [                            ]  │
│  Gender:         [                          ▾]   │
│  Additional info:[                            ]  │
│                  [                            ]  │
│                                                  │
│              [ Screen & Discount ]               │
│                                                  │
│  Status: 11 sources loaded, last refresh 3h ago  │
│  Records: 28,431 individuals indexed             │
└──────────────────────────────────────────────────┘
```

After submit: live progress (screening → 80 candidates → dedup to 28 → auto-clearing 16 → analyzing 12 with AI → done), then inline results table (same columns as Sheet 2 of the XLSX) with "Download XLSX" and "Download JSON" buttons. Screening history sidebar shows past reports.

### FastAPI Backend

```
POST /api/screen          — submit screening, returns report JSON
GET  /api/screen/{id}     — retrieve past screening by audit ID
GET  /api/status          — data freshness, record counts
POST /api/refresh         — trigger manual data refresh
GET  /api/history         — list past screenings with pagination
```

---

## Configuration

Single `.env` file:

```bash
# Claude API (required — user provides their own key)
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-6

# Data refresh
AUTO_REFRESH=true
REFRESH_SCHEDULE="0 2 * * *"   # 2am daily

# Storage
DATA_DIR=/data
AUDIT_RETENTION_DAYS=2555       # 7 years

# Sources (all enabled by default; disable specific ones if needed)
# DISABLED_SOURCES=au_dfat,interpol_red

# Matching thresholds
FUZZY_THRESHOLD=60              # minimum rapidfuzz score to consider (0-100)
MAX_CANDIDATES=20               # max candidates per screening per source

# Server
HOST=0.0.0.0
PORT=8080
```

---

## Deployment

Single Docker image. No external dependencies except Claude API access.

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y libicu-dev pkg-config
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
WORKDIR /app
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

### Run it

```bash
# ZAR internal
docker run -p 8080:8080 -v aml-data:/data -e ANTHROPIC_API_KEY=sk-ant-... ghcr.io/zarpay/aml-discounter:latest

# Hand to partner
docker run -p 8080:8080 -v aml-data:/data -e ANTHROPIC_API_KEY=<their-key> ghcr.io/zarpay/aml-discounter:latest
```

First launch fetches all lists (~5-10 minutes depending on connection). Subsequent starts use cached data. Auto-refresh runs nightly.

### System requirements

- 4GB RAM (OFAC XML parsing peaks at ~2GB during initial ingest)
- 10GB disk (list data + SQLite + audit logs)
- Network access to source URLs + `api.anthropic.com`
- No GPU, no cluster, no external databases

---

## Open-Source Strategy

- **Code:** MIT license. `github.com/zarpay/aml-discounter` (public).
- **Data:** Fetched at runtime by each instance from authoritative sources. We redistribute nothing.
- **Branding:** Generic. No ZAR-specific config or branding in the open-source release.
- **README:** Clear setup instructions, source list with URLs, sample report, architecture diagram.
- **Payoff:** Credibility in fintech compliance space. Relationship builder with partners. Potential to become the standard open-source AML screening tool for smaller fintechs.

---

## What We're Explicitly Not Building (v1)

- Real-time screening integrated into onboarding flow
- Entity/business/vessel screening (individuals only)
- Transaction monitoring
- Adverse media screening
- Ongoing monitoring / automatic rescreen on list update
- STR/CTR/SAR filing
- Multi-tenant SaaS
- Custom scoring models or ML training

---

## Python Dependencies

```
# Core
anthropic>=1.0.0        # Claude API
fastapi>=0.110.0        # Web server
uvicorn>=0.27.0         # ASGI server
httpx>=0.27.0           # HTTP client (async, for fetching lists)

# Parsing
lxml>=5.0.0             # XML parsing (streaming iterparse for OFAC)
openpyxl>=3.1.0         # XLSX parsing (Australia DFAT)
pyyaml>=6.0             # YAML parsing (US Congress)

# Matching
rapidfuzz>=3.6.0        # Fuzzy string matching (Jaro-Winkler, token sort)
jellyfish>=1.0.0        # Phonetic algorithms (Double Metaphone)
PyICU>=2.12             # Unicode transliteration (Arabic/Cyrillic → Latin)

# Report generation
openpyxl>=3.1.0         # XLSX report generation (also used for AU DFAT parser)

# Utilities
orjson>=3.9.0           # Fast JSON (for large list parsing)
click>=8.1.0            # CLI
python-dotenv>=1.0.0    # .env config
```

No heavyweight dependencies. No scikit-learn, no ElasticSearch, no DuckDB, no TensorFlow.

---

## Build Plan

### Phase 1: Data + Matching

- Project scaffold (FastAPI, SQLite, Docker)
- OFAC SDN + Consolidated Advanced XML streaming parser
- UN Consolidated, EU FSF, UK Sanctions parsers
- Unified schema, SQLite tables, FTS5 index
- Fetcher with change detection (ETag/hash)
- Canada SEMA, Switzerland SECO, Australia DFAT parsers
- Wikidata PEP SPARQL fetcher (per-country queries)
- US Congress, UK Parliament, EU Parliament, FBI parsers
- Matcher engine: multi-pass query (exact → phonetic → token-sort → fuzzy re-rank)
- ICU transliteration pipeline (Arabic/Cyrillic → Latin)
- Cross-list deduplication
- Pre-score filter (gender/DOB/ID auto-clear)

### Phase 2: Discounting + Reports

- Claude prompt with contradiction-detection pattern
- Batched inference (all ambiguous matches in one call)
- Structured JSON output parsing
- XLSX report generation (3 sheets: Summary, Matches, Audit)
- Conditional formatting, auto-filter, freeze panes
- JSON output
- Test against known false positives and true positives from ZAR history

### Phase 3: Packaging

- Web UI (FastAPI + single-page HTML form + progress + results table)
- CLI (--input, --output, --bulk, --refresh, --status)
- Dockerize
- README, sample screenings, architecture docs
- Data freshness monitoring (assert minimum entity counts per source)

### Phase 4 (optional): Harden

- Interpol Red Notices (solve 403 / complex querying)
- Australia DFAT (solve geo-blocking)
- Test suite: 50 known-positive + 50 known-negative + 50 ambiguous
- Bulk screening mode (CSV in → XLSX reports out)
- Monitoring dashboard (data freshness, screening counts, flag rates)

---

## What's Genuinely Harder Than It Looks

1. **OFAC Advanced XML parser.** 117MB file with 4-section join architecture. Budget a full day. Reference OpenSanctions' 828-line parser but strip to ~200 lines (individuals only). Use `lxml.etree.iterparse` for streaming.

2. **Name transliteration.** PyICU handles it but installation is non-trivial (requires `libicu-dev` system package). Arabic names have 30+ legitimate English spellings for common names ("Muhammad" alone). Must normalize both sides (user input AND list entries) before comparison.

3. **PEP matching is ambiguous by nature.** A customer named "Imran Khan" might actually BE the politician Imran Khan. The LLM has to reason carefully here. Mitigation: PEP matches always recommend human review unless there's a clear age/gender/nationality contradiction.

4. **Date parsing across sources.** Every source uses different date formats. OFAC uses range structures, UN has EXACT/APPROXIMATELY/BETWEEN types, EU has circa flags, Australia has corrupt formats ("196719611973"). Build a robust date normalizer once and reuse.

5. **List format changes will break parsers.** OFAC changed XML namespaces in May 2024. UK consolidated lists in Jan 2026. SECO restructured their entire website. Mitigation: on each fetch, assert minimum entity counts (e.g., OFAC SDN must have >6,000 individuals). If count drops below minimum, alert and refuse to use stale data.

6. **De-listings.** When someone is removed from a list, daily refresh handles it (we rebuild the index from fresh data). But audit trail must show "at time of screening, this person was on the list." Store source file SHA256 per screening.

None are blockers. All are known and manageable.

---

## Access Requirements

**Nothing to procure, register for, or get approval on.**

| Requirement | Status |
|-------------|--------|
| OFAC SDN/Consolidated XML | Public, no auth |
| UN Consolidated XML | Public, no auth |
| EU FSF XML | Public (static token in URL) |
| UK Sanctions XML | Public, no auth |
| Canada SEMA XML | Public, no auth |
| Switzerland SECO XML | Public, no auth |
| Australia DFAT XLSX | Public (may be geo-restricted) |
| Wikidata SPARQL | Public, no auth |
| US Congress YAML | Public, CC0 |
| UK Parliament API | Public, no auth |
| EU Parliament XML | Public, no auth |
| FBI API | Public, no auth |
| Anthropic API key | Already have (ZAR's key in ~/.secrets) |
| Python 3.12 | Already have |
| Docker | Already have |

**We are ready to build.**
