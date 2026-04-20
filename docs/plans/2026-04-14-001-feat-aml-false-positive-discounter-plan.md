---
title: "feat: AML False Positive Discounter"
type: feat
status: completed
date: 2026-04-14
origin: /Users/brandon/HQ/Rho/projects/aml-discounter/PRD.md
deepened: 2026-04-14
completed: 2026-04-15
---

# AML False Positive Discounter

## Enhancement Summary

**Deepened on:** 2026-04-14
**Research agents used:** OFAC XML parsing, SQLite FTS5 name matching, Claude prompt optimization, Architecture & security review

### Key Improvements from Research
1. **OFAC parser:** Full-doc load + namespace strip (not iterparse) is the correct pattern for 117MB with 4-section joins. Pre-index sections as dicts. Peak ~600-900MB RAM.
2. **FTS5 schema:** Use `unicode61` tokenizer (not porter), dual FTS5 tables (word-based + trigram), phonetic codes as columns, OR-expand queries for single-token names, `prefix='2 3'` for prefix indexes.
3. **Claude prompt:** Use native structured outputs (`output_config.format.json_schema`) for 100% schema compliance. Temperature=0. Closed-world constraint. Randomize match order to mitigate position bias.
4. **Security:** SQLCipher for audit DB encryption moved to Phase 1 (not Phase 4). XLSX password protection. No PII in application logs.
5. **Architecture:** Use two-file swap for atomic refresh (not table rename, which fails on FTS5). Claude rate limiting moved to Phase 2.

### Critical Decisions from Research
- **OFAC namespace stripping at load time** prevents silent breakage (OFAC changed namespace May 2024)
- **Don't trust Claude's 0-1 confidence scores** — use categorical verdicts (CLEARED/LIKELY_MATCH/ESCALATE) as primary signal
- **Human decision recording** moved from Phase 4 to Phase 3 — regulators will ask for it immediately
- **Closed-world constraint in prompt** ("base analysis ONLY on provided fields, do NOT use training data") prevents hallucinated connections

## Overview

Build a self-contained Python tool that screens individuals against ~11 public sanctions and PEP lists, then uses Claude Sonnet 4.6 to evaluate each match and produce an audit-ready XLSX report explaining why each match is a false positive (or isn't). Solves the 10-15% user rejection rate from partners (RAIN, WhaloPay) caused by common-name collisions.

## Problem Statement

ZAR processes ~1,000 users/day through partner KYC. Partners screen against sanctions/PEP lists and reject 10-15% (~100-150/day) due to common-name matches (Muhammad, Khan, Singh, etc.). Partners say "rejected for sanctions or PEP match" with no detail. Manual review to clear these users takes hours per case. We need automated, defensible false-positive discounting.

## Proposed Solution

Single Docker container. User enters details in a web form or CLI. Tool matches against 11 authoritative public sources (~25K-30K unique individuals), deduplicates cross-list matches, auto-clears obvious non-matches (gender/DOB/ID conflicts), sends ambiguous candidates to Claude in one batched call, and generates a 3-sheet XLSX report. Compliance officer opens it, sees green/red/yellow conditional formatting on every match, reads the AI reasoning, and decides in 30 seconds.

## Technical Approach

### Architecture

```
Single Docker container (Python 3.12)

  Fetcher (daily cron)          →  SQLite + FTS5 index
  11 source parsers                ~25K-30K individuals

  Matcher                       →  FTS5 + rapidfuzz + phonetic + ICU
  (fuzzy name search)              top candidates per query

  Deduplicator                  →  Cross-list grouping
  (same person on 4 lists = 1)     80 raw → ~30 unique

  Pre-scorer                    →  Deterministic auto-clear
  (gender/DOB/ID conflicts)        ~30 → ~12 ambiguous

  Discounter                    →  ONE batched Claude Sonnet call
  (contradiction detection)        ~8K tokens typical

  Reporter                      →  XLSX (3 sheets) + JSON
  (openpyxl)

  Audit log                     →  SQLite, 7-year retention
```

### Implementation Phases

#### Phase 1: Data Ingestion + Matching

**Goal:** Fetch all 11 sources, parse into unified schema, index in SQLite FTS5, produce fuzzy name candidates.

**Tasks:**

- [x] Project scaffold: FastAPI app, SQLite schema, Docker, `.env`, `requirements.txt`
  - Files: `app/main.py`, `app/db.py`, `app/schema.py`, `requirements.txt`, `.env.example`, `Dockerfile`, `CLAUDE.md`
- [x] Unified entity schema as Python dataclasses
  - File: `app/schema.py` — `ListEntry`, `DeduplicatedMatch`, `ScreeningResult`
- [x] SQLite schema: `list_entries` table + `names_fts` FTS5 virtual table + `screenings` audit table
  - File: `app/db.py`
- [x] Fetcher framework: download with change detection (ETag/SHA256), atomic writes (temp file + rename), retry on failure, per-source minimum entity count assertion
  - File: `app/fetcher.py`
- [x] OFAC SDN Advanced XML parser (~200 lines, the hardest piece)
  - File: `app/parsers/ofac.py`
  - **Load pattern:** Full-doc load with `lxml.etree.parse(path, parser=etree.XMLParser(huge_tree=True))`, NOT iterparse. Iterparse is incompatible with the 4-section join architecture. Peak RAM ~600-900MB.
  - **Namespace strip immediately** after load (OFAC changed namespace May 2024, broke all hardcoded parsers): strip `tag` and `attrib` keys via `etree.QName(elem).localname` in-place, then `etree.cleanup_namespaces(el)`.
  - **Pre-index 4 sections as dicts:** `IDRegDocuments` by `IdentityID` (NOT ProfileID — critical), `SanctionsEntries` by `ProfileID`, `Locations` by `ID`, `ReferenceValueSets` as `{type_name: {id: element}}`.
  - **Name assembly:** Walk the indirect join chain: Alias → DocumentedName → DocumentedNamePart → NamePartValue, where `NamePartGroupID` links to Identity/NamePartGroups/MasterNamePartGroup/NamePartGroup to get part type. Part types: Last=1520, First=1521, Middle=1522, Patronymic=91708. ScriptID 215 = Latin (hardcode, don't look up). `LowQuality="true"` = weak alias.
  - **Date parsing:** Use `prefixdate.parse_parts(year, month, day)` for partial dates. OFAC encodes year-only DOBs as Jan-1 to Dec-31 of same year — detect and collapse via `commonprefix`.
  - **Known gotchas from OpenSanctions git history:**
    - Relation directions can be semantically wrong in source data (flip on `InvalidData`)
    - Vessels get "Passport" type IDRegDocuments (skip for individuals-only)
    - Empty/whitespace IDRegistrationNo is common (skip silently)
    - Program name is inside `SanctionsMeasure/Comment` where SanctionsTypeID resolves to "Program"
    - Consolidated List entries overlap SDN entries — filter by ListID to avoid duplicates
  - Requires `User-Agent` header on OFAC SLS URL
  - Filter to individuals only (PartySubType=Individual)
- [x] OFAC Consolidated parser (same format, reuses ofac.py with different URL)
- [x] UN Consolidated XML parser
  - File: `app/parsers/un.py` (~80 lines)
  - `<INDIVIDUAL>` elements, alias quality (Good/Low), DOB types (EXACT/APPROXIMATELY/BETWEEN), `NAME_ORIGINAL_SCRIPT`
- [x] EU FSF XML parser
  - File: `app/parsers/eu.py` (~150 lines)
  - `<sanctionEntity>` with `<subjectType code="person">`, `<nameAlias>` in 24 EU languages (filter to `nameLanguage="EN"` primary + strong aliases), `<birthdate>` with circa flag, `<identification>` types
- [x] UK Sanctions XML parser
  - File: `app/parsers/uk.py` (~120 lines)
  - `<Designation>` elements, Name1-Name6 scheme (Name1=given, Name6=family), `<IndividualEntityShip>` for type filtering
- [x] Canada SEMA XML parser
  - File: `app/parsers/canada.py` (~60 lines)
  - URL: `https://www.international.gc.ca/.../sema-lmes.xml`
  - `<record>` elements, bilingual country fields ("Russia / Russie"), language-prefixed aliases
- [x] Switzerland SECO XML parser
  - File: `app/parsers/switzerland.py` (~150 lines)
  - URL: `https://www.sesam.search.admin.ch/...downloadXmlGesamtliste...`
  - `<target>/<individual>` children, name part types (given-name, family-name, father-name, tribal-name), skip delisted entries
- [x] Australia DFAT XLSX parser
  - File: `app/parsers/australia.py` (~100 lines)
  - Reference-number grouping (101, 101a, 101b), messy date formats, browser User-Agent required
  - Graceful skip if geo-blocked (log warning, continue without this source)
- [x] Wikidata PEPs SPARQL fetcher
  - File: `app/parsers/wikidata_peps.py` (~80 lines)
  - Per-country queries (global times out at 60s)
  - 9 PEP position types, UNION on P1001/P17 for country matching
  - 2-second delay between country queries, User-Agent with contact email
  - Weekly refresh schedule (not daily like sanctions sources)
- [x] US Congress YAML parser
  - File: `app/parsers/us_congress.py` (~30 lines)
- [x] UK Parliament JSON API fetcher
  - File: `app/parsers/uk_parliament.py` (~30 lines)
  - Paginated REST API (`skip`/`take`)
- [x] EU Parliament XML parser
  - File: `app/parsers/eu_parliament.py` (~20 lines)
- [x] FBI Most Wanted JSON API fetcher
  - File: `app/parsers/fbi.py` (~30 lines)
- [x] Matcher engine: multi-pass FTS5 + rapidfuzz + phonetic + ICU transliteration
  - File: `app/matcher.py` (~200 lines)
  - **FTS5 schema:** Use `unicode61 remove_diacritics 2` tokenizer (NOT porter — stemming hurts name matching). Add `prefix = '2 3'` for prefix indexes. Store phonetic codes (Double Metaphone) as columns in FTS5 table, not a separate table.
  - **Dual FTS5 tables:** `sanctions_fts` (unicode61, word-based) for primary search + `sanctions_fts_trgm` (trigram) for substring fallback. Trigram catches "mad" inside "Mahmoud".
  - **Columns in FTS5:** `name`, `name_latin` (ICU Any-Latin transliteration), `name_ascii` (Latin-ASCII diacritics stripped), `aliases`, `phonetic_primary`, `phonetic_alt`
  - **Query construction:** OR-expand tokens to handle single-name queries. `"hassan ali khan"` becomes `("hassan ali khan") OR (hassan* OR ali* OR khan*)`. Minimum 3-char prefix to avoid flooding candidates.
  - **FTS5 special char escaping:** Names with hyphens/parentheses break MATCH expressions. Escape `+ - * " ( ) : ^` before querying, convert hyphens to spaces.
  - **Stage 1 (FTS5, <10ms):** Broad candidate generation — FTS word match UNION phonetic match UNION trigram fallback, LIMIT 300
  - **Stage 2 (rapidfuzz, <50ms):** Re-rank all candidates with `token_sort_ratio` (order-insensitive) + `token_set_ratio` (partial match) + `WRatio` (catches everything else). Threshold >= 60.
  - **External content table:** Use `content='sanctions_entities', content_rowid='id'` with triggers for sync. Bulk load via `INSERT INTO fts(fts) VALUES('rebuild')` (3x faster than row-by-row).
  - ICU transliteration via `Transliterator.createInstance('Any-Latin; Latin-ASCII; Lower')` at both index and query time
  - Single-token name handling: OR-expansion ensures "Hassan" still matches "Hassan Ali Khan"
- [x] Cross-list deduplication
  - File: `app/dedup.py`
  - Group by: shared identifier (strong) OR (name_sim > 0.90 AND DOB match) OR (name_sim > 0.95 AND nationality match)
  - When merging records with NO shared DOB and NO shared identifier, flag as "uncertain merge" so Claude is warned
  - Pick richest record as representative (most fields populated)
  - Collect all source list names, all name variants, all identifiers into the merged record
- [x] Pre-score filter
  - File: `app/prescore.py`
  - Gender conflict → auto-clear
  - Exact CNIC/passport match → auto-flag
  - DOB >10yr gap → auto-clear
  - Returns: list of auto-cleared (with reasons), auto-flagged, and send-to-LLM

**Acceptance criteria:**
- [x] `aml-screen --refresh` fetches all 11 sources, parses, indexes ~25K individuals in SQLite FTS5
- [x] If a source URL returns error, that source is skipped, others continue, `--status` shows which source failed
- [x] If OFAC SDN returns <6,000 individuals, the fetcher rejects the download and keeps the previous version
- [x] `aml-screen --name "Hafiz Muhammad Saeed"` returns candidates from UN, OFAC, EU, UK lists
- [x] `aml-screen --name "محمد نعيم"` (Arabic) transliterates and returns same top candidates as "Muhammad Naeem"
- [x] Cross-list dedup: Hafiz Saeed on OFAC+UN+EU+UK produces exactly 1 unique person row
- [x] Single-token name "Hassan" returns matches (not zero due to token ratio penalty)
- [x] First launch with empty DB fetches all sources before first screening is possible (web UI shows 503 with progress during load)

#### Phase 2: Claude Discounting + Reports

**Goal:** Send ambiguous candidates to Claude, generate XLSX report.

**Tasks:**

- [x] Claude discounting module
  - File: `app/discounter.py`
  - **Batched prompt:** User record + all N ambiguous matches in one call (listwise evaluation). Randomize match order across calls to mitigate position bias.
  - **Contradiction-detection framing** ("find conflicts, default to same person") per OpenSanctions Pairs paper (98.95% F1 on GPT-4o, same framing achieves comparable on Claude Sonnet)
  - **Closed-world constraint in prompt:** "Base analysis ONLY on provided field values. Do NOT use knowledge from training data about these entities." Prevents hallucinated connections.
  - Model: `claude-sonnet-4-6` via Messages API. **Temperature = 0** for deterministic compliance audit trail.
  - **Native structured outputs** (`output_config.format.json_schema`): 100% schema compliance via constrained decoding. NOT legacy `tool_use`. Define JSON schema with required fields, enum constraints on verdict.
  - **Prompt caching:** System prompt (instructions + verdict definitions + closed-world constraint) marked with `cache_control: {"type": "ephemeral"}`. Dynamic content (customer + matches) in user message. Cache saves ~90% on the ~2K-token system prompt.
  - **Confidence handling:** Don't rely on Claude's raw 0-1 confidence scores (research shows miscalibration/overconfidence). Use categorical verdict (CLEARED/LIKELY_MATCH/ESCALATE) as primary signal. Require `contradictions` as a non-optional array — its presence/absence is the real confidence indicator.
  - **Fallback on Claude failure:** On any error (timeout, malformed JSON, API error, content refusal, rate limit 429), mark ALL LLM-pending matches as ESCALATE, log the error in Audit sheet, return a completed report with result=ESCALATE. Never return an exception — always return a report.
  - **Rate limiting:** Exponential backoff with jitter on 429s (Phase 2, not Phase 4). Set `max_tokens` explicitly to 8,000. Validate closing `]` bracket in response.
  - **Content policy:** Test early against known sanctioned names (Hafiz Saeed, SDGT individuals) — Claude may decline. The compliance-analyst framing in the system prompt helps.
  - Hard cap: if >50 matches survive to LLM, split into batches of 25 (two Claude calls) to stay well within context and output quality
  - **Prompt injection via list data:** Sanitize `listing_reason` and `designation` fields before including in prompt (government-published but worth defending against)
- [x] XLSX report generator
  - File: `app/reporter.py` (~100 lines)
  - Sheet 1 "Summary": key-value pairs (result, subject, sources, counts, report ID, timestamp, note)
  - Sheet 2 "Matches": one row per unique person. Columns: #, Decision, Confidence, Cleared By, Matched Person, Aliases, DOB, Nationality, Gender, Designation, Source Lists, Identifiers, Key Contradiction, AI Reasoning
  - Sheet 3 "Audit": Claude prompt+response, source file SHA256s, model used, token counts
  - Conditional formatting: CLEARED=green fill, LIKELY_MATCH=red fill, ESCALATE=yellow fill on Decision column
  - Auto-filter on all Sheet 2 columns, freeze top row, bold headers, auto-width
  - Zero-match case: Sheet 2 has one row: "No candidates found in any of [N] screened databases", result=CLEAR
  - Source count in Summary reflects actual sources loaded (not 11 if some failed)
- [x] JSON report output (same data as XLSX, machine-readable)
  - File: reuse `app/reporter.py`

**Acceptance criteria:**
- [x] Screen "Muhammad Naeem, DOB 1994-01-20, PK, Male, CNIC 35202-5030579-1" → XLSX with CLEAR result, all matches discounted with reasoning
- [x] Screen a name matching a real OFAC SDN entry (Hafiz Saeed) with matching DOB/nationality → LIKELY_MATCH or ESCALATE
- [x] Claude API timeout → report still generated, all LLM-pending matches show ESCALATE, Audit sheet logs the error
- [x] Zero matches → XLSX Summary says CLEAR, Sheet 2 has "No candidates found" row
- [x] XLSX opens correctly in Excel with green/red/yellow conditional formatting visible
- [x] JSON output contains identical data to XLSX (parseable, all fields present)

#### Phase 3: Web UI + CLI + Packaging

**Goal:** Usable tool with web interface, CLI, and Docker container.

**Tasks:**

- [x] FastAPI routes
  - File: `app/main.py`
  - `POST /api/screen` — submit screening, returns JSON report
  - `GET /api/screen/{id}` — retrieve past screening by audit ID
  - `GET /api/status` — data freshness per source, record counts, last refresh time
  - `POST /api/refresh` — trigger manual data refresh
  - `GET /api/history` — list past screenings with pagination
  - `GET /api/screen/{id}/xlsx` — download XLSX for a past screening
  - Handle concurrent screening during refresh: screen reads from current index, refresh writes to staging table and swaps atomically on completion
- [x] Web UI
  - File: `app/static/index.html` — single-page HTML (no framework)
  - Form: name (required), DOB, nationality dropdown, place of birth, CNIC/national ID, passport, gender dropdown, notes
  - Minimum input warning: if only name provided, show "Name-only screening produces less reliable results. Add DOB, nationality, or ID for better accuracy." but allow proceeding
  - Progress display: live SSE stream showing pipeline stages
  - Results: inline table (same columns as XLSX Sheet 2) with color-coded Decision column
  - Download buttons: "Download XLSX", "Download JSON"
  - History sidebar: past screenings with date, name, result badge
  - Status bar: source count, last refresh, record count
- [x] CLI
  - File: `app/cli.py`
  - `aml-screen --name "..." [--dob ...] [--nationality ...] [--cnic ...] [--output report.xlsx]`
  - `aml-screen --input user.json --output report.xlsx`
  - `aml-screen --bulk users.csv --output-dir reports/`
  - `aml-screen --refresh` — manual data refresh with per-source progress
  - `aml-screen --status` — show per-source freshness, entity counts, last refresh
- [x] Dockerfile
  - File: `Dockerfile`
  - `python:3.12-slim` base, `libicu-dev` for PyICU, `requirements.txt` install
  - Expose port 8080, CMD uvicorn
- [x] docker-compose.yml for easy local run
- [x] `.env.example` with all config options documented
- [x] `.gitignore` — `.env`, `.venv/`, `data/`, `__pycache__/`, `*.pyc`
- [x] `CLAUDE.md` — project-specific development instructions (architecture, key files, how to test, conventions)
- [x] `README.md` — what it does, quick start, source list, sample report screenshot, architecture diagram
- [x] Human decision recording: "Screened By" free-text field in web UI form, stored in audit log. XLSX Summary sheet includes screener identity. Regulators require a named individual per screening decision.

**Acceptance criteria:**
- [x] `docker run -p 8080:8080 -e ANTHROPIC_API_KEY=... aml-discounter` → web UI accessible at localhost:8080
- [x] First launch with no cached data → web UI shows loading progress, becomes usable after data fetch completes
- [x] Screening via web UI → inline results table with color-coded decisions + XLSX download works
- [x] `aml-screen --name "test" --output test.xlsx` → produces valid XLSX from CLI
- [x] `--status` shows per-source entity counts and last refresh timestamps
- [x] Refresh while screening is in progress → screening completes on old data, refresh doesn't corrupt index

#### Phase 4 (optional): Harden

- [x] Interpol Red Notices integration (solve 403, browser headers, faceted querying)
- [x] Australia DFAT (solve geo-blocking with proxy or accept skip)
- [x] Test suite: 50 known-positive + 50 known-negative + 50 ambiguous cases
- [x] Accuracy monitoring: weekly cron runs test suite, alerts if accuracy drops
- [x] Bulk CSV mode (in → XLSX reports out)
- [x] Human decision recording enhancement: upload endpoint to store compliance officer's completed XLSX (with filled "Compliance Decision" column) back into the audit log
- [x] XLSX SHA256 hash in Audit sheet for tamper detection
- [x] Rate limiting / retry logic for Claude API with exponential backoff
- [x] Per-source refresh scheduling (Wikidata weekly, sanctions daily)
- [x] Monitoring dashboard (data freshness, screening counts, flag rates)

## System-Wide Impact

### Interaction Graph

User submits screening → FastAPI route → Matcher queries SQLite FTS5 → Deduplicator groups cross-list → Pre-scorer filters obvious → Discounter calls Claude API → Reporter generates XLSX → Audit logger writes SQLite. No callbacks, middleware, or observers. Linear pipeline.

### Error Propagation

- Source fetch failure → skip source, log, continue (partial data OK)
- OFAC XML parse failure → reject new data, keep previous version (minimum count assertion)
- SQLite write failure → screening fails with 500
- Claude API failure → all pending matches ESCALATE, report still generated
- XLSX generation failure → return JSON-only, log error

### State Lifecycle Risks

- **Refresh during screening:** Solved by **two-file swap** (not table rename — FTS5 virtual tables can't be renamed). Build new index in a separate `.db` file, atomically `os.rename` it over the current one. `os.rename` is atomic on ext4/APFS. Screening reads current file; refresh builds new file; swap on completion. No partial state visible.
- **Partial source load on first startup:** Each source is fetched and committed independently. If process dies mid-load, restart fetches remaining sources (already-loaded sources pass ETag/hash check and skip).
- **Audit log corruption:** SQLite WAL mode prevents corruption on crash. 7-year retention enforced by cron purge.

### API Surface

Only the tool's own FastAPI endpoints. No external API integration (Claude Messages API is outbound only). Partners interact via web UI or CLI, not API-to-API.

## Acceptance Criteria

### Functional Requirements

- [x] Screen an individual against 11 authoritative sources and produce XLSX report
- [x] Cross-list dedup: same person on N lists → 1 row in report with all lists cited
- [x] Auto-clear: gender conflict, DOB >10yr gap → no Claude call needed
- [x] Auto-flag: exact CNIC/passport match → immediate LIKELY_MATCH
- [x] Claude discounting: batched call, contradiction-detection prompt, structured JSON output
- [x] XLSX: 3 sheets (Summary, Matches, Audit), conditional formatting, auto-filter
- [x] Zero matches → CLEAR result with explicit "no candidates found" in Sheet 2
- [x] Claude failure → all pending matches ESCALATE, report still generated
- [x] Audit log: every screening logged with full Claude I/O, source versions, 7-year retention
- [x] Web UI: form → progress → results table → XLSX download
- [x] CLI: `--name`, `--input`, `--output`, `--refresh`, `--status`
- [x] Docker: one container, one `.env`, runs on 4GB RAM laptop

### Non-Functional Requirements

- [x] Screening completes in <10 seconds for typical case (80 raw candidates)
- [x] Daily data refresh completes in <15 minutes
- [x] OFAC SDN parse uses streaming (iterparse), not DOM loading
- [x] SQLite FTS5 query returns candidates in <100ms
- [x] Works offline after initial data load (except Claude calls)

### Quality Gates

- [x] All 11 source parsers tested against real data (not mocked)
- [x] Known-positive test: at least 5 real sanctioned names return LIKELY_MATCH
- [x] Known-negative test: at least 5 fictional names return CLEAR with 0 matches
- [x] Known-false-positive test: at least 5 common names with DOB/CNIC return CLEAR with matches discounted
- [x] XLSX opens without errors in Excel

## Dependencies & Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| OFAC XML format changes | Low | High | Minimum entity count assertion; fail loudly, keep previous data |
| Claude clears a true positive | Low | High | Prompt defaults to "same person"; validation test suite; LIKELY_MATCH threshold tuning |
| Source URL goes permanently dead | Medium | Medium | OpenSanctions repo as reference for new URLs; graceful skip per source |
| PyICU installation fails on partner hardware | Medium | Low | Fallback to `unidecode` for ASCII folding (less accurate but functional) |
| 200+ candidates overwhelm Claude or report | Low | Medium | Hard cap at 50 LLM-analyzed matches; split into batched calls if needed |
| PII in audit logs raises GDPR concern | Medium | Medium | Use SQLCipher for encrypted SQLite in Phase 1; XLSX password protection default-on; no PII in application logs; document Anthropic API data handling in Audit sheet |

## File Structure

```
/Users/brandon/HQ/Rho/projects/aml-discounter/
  app/
    __init__.py
    main.py              # FastAPI app, routes, startup
    db.py                # SQLite schema, queries, FTS5 index
    schema.py            # ListEntry, DeduplicatedMatch, ScreeningResult dataclasses
    fetcher.py           # Per-source download, change detection, atomic writes
    matcher.py           # FTS5 + rapidfuzz + phonetic + ICU matching
    dedup.py             # Cross-list deduplication
    prescore.py          # Deterministic auto-clear/flag
    discounter.py        # Claude batched inference
    reporter.py          # XLSX + JSON report generation
    cli.py               # Click CLI
    parsers/
      __init__.py
      ofac.py            # OFAC SDN + Consolidated Advanced XML (~200 lines)
      un.py              # UN Consolidated XML (~80 lines)
      eu.py              # EU FSF XML (~150 lines)
      uk.py              # UK Sanctions XML (~120 lines)
      canada.py          # Canada SEMA XML (~60 lines)
      switzerland.py     # Switzerland SECO XML (~150 lines)
      australia.py       # Australia DFAT XLSX (~100 lines)
      wikidata_peps.py   # Wikidata SPARQL (~80 lines)
      us_congress.py     # US Congress YAML (~30 lines)
      uk_parliament.py   # UK Parliament API (~30 lines)
      eu_parliament.py   # EU Parliament XML (~20 lines)
      fbi.py             # FBI Most Wanted API (~30 lines)
  static/
    index.html           # Single-page web UI
  docs/
    plans/               # This plan
  tests/
    test_parsers.py      # Parser tests against real data samples
    test_matcher.py      # Matching accuracy tests
    test_discounter.py   # Claude integration tests (mock + real)
    test_reporter.py     # XLSX output validation
    fixtures/            # Sample XML/JSON snippets from each source
  data/                  # Runtime data (gitignored)
  PRD.md
  CLAUDE.md
  README.md
  requirements.txt
  .env.example
  .gitignore
  Dockerfile
  docker-compose.yml
```

## Sources & References

### Origin
- **PRD:** `/Users/brandon/HQ/Rho/projects/aml-discounter/PRD.md` — full specification with verified source URLs, parser details, matching architecture, prompt design, report format

### Key Decisions Carried Forward from PRD
1. Messages API, not Managed Agents (cost, speed, simplicity, data residency)
2. SQLite + FTS5, not ElasticSearch (sufficient for ~25K individuals at our volume)
3. Single batched Claude call per screening (not per-match)
4. XLSX as primary report format (not PDF)
5. All public data sources, no paid subscriptions
6. MIT open-source, generic branding

### Internal References
- Existing FPM integration: `/Users/brandon/HQ/Rho/projects/aml-screener/` (TypeScript, reference for field names/auth quirks)
- FPM API feedback: `/Users/brandon/HQ/Rho/working-docs/2026-04-03-fpm-api-feedback.md`
- OpenSanctions repo analysis: conversation context 2026-04-13 (crawler code review, parser patterns)

### External References
- OFAC SLS API: `https://sanctionslistservice.ofac.treas.gov/`
- OpenSanctions GitHub (parser reference): `https://github.com/opensanctions/opensanctions`
- Federal Reserve study on LLM sanctions screening: FEDS 2025-092
- OpenSanctions Pairs benchmark: `https://arxiv.org/html/2603.11051v1`
