# AML Discounter

Self-contained AML false positive discounting tool. Screens individuals against ~11 public sanctions/PEP lists and uses Claude Sonnet 4.6 to evaluate matches.

## Architecture

```
Fetcher → SQLite FTS5 → Matcher → Dedup → Pre-score → Claude → XLSX Report
```

Single Python app. FastAPI + SQLite + Claude Messages API. No external services besides Claude.

## Key Files

| File | Purpose |
|------|---------|
| `app/main.py` | FastAPI routes, screening pipeline orchestration |
| `app/db.py` | SQLite schema (audit + FTS5 index), two-file swap for atomic refresh |
| `app/schema.py` | `ListEntry`, `DeduplicatedMatch`, `ScreeningResult` dataclasses |
| `app/fetcher.py` | Downloads all sources, parses, indexes into staging DB |
| `app/matcher.py` | FTS5 + rapidfuzz + phonetic + ICU matching |
| `app/dedup.py` | Cross-list deduplication (same person on 4 lists = 1 row) |
| `app/prescore.py` | Deterministic auto-clear (gender/DOB/ID conflicts) |
| `app/discounter.py` | Claude batched inference with contradiction-detection prompt |
| `app/reporter.py` | XLSX (3 sheets) + JSON report generation |
| `app/cli.py` | Click CLI |
| `app/parsers/` | 12 source-specific parsers |

## Dev Setup

```bash
cd /Users/brandon/HQ/Rho/projects/aml-discounter
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # add ANTHROPIC_API_KEY
```

## Run

```bash
# Start web UI
uvicorn app.main:app --host 0.0.0.0 --port 8080

# CLI
python -m app.cli refresh   # fetch all data sources
python -m app.cli screen --name "Muhammad Naeem" --dob 1994-01-20 --nationality PK
python -m app.cli status    # show data freshness
```

## Testing

```bash
# Verify parsers work against real data
python -m app.cli refresh
python -m app.cli status

# Screen a known sanctioned person (should flag)
python -m app.cli screen --name "Hafiz Muhammad Saeed" --nationality PK

# Screen a common name (should clear with reasoning)
python -m app.cli screen --name "Muhammad Ali" --dob 1995-03-15 --nationality PK --gender Male
```

## Data Sources (11 verified)

OFAC SDN, OFAC Consolidated, UN Security Council, EU Financial Sanctions, UK Sanctions, Canada SEMA, Switzerland SECO, Wikidata PEPs, US Congress, UK Parliament, EU Parliament, FBI Most Wanted

All free, all public, fetched directly from authoritative publishers. No paid subscriptions.

## MCP Server

Exposes the screening pipeline as MCP tools for AI agents (e.g., ZAR customer support agent).

**Tools:**
- `screen_customer` — run full screening, return summarized verdict
- `get_screening_report` — generate XLSX audit report, return file path
- `get_screening_details` — retrieve full match details for a past screening

**Run (stdio for Claude Code/Desktop):**
```bash
.venv/bin/python -m app.mcp_server
```

**Claude Code MCP config:**
```json
{
  "mcpServers": {
    "aml-discounter": {
      "command": "/Users/brandon/HQ/Rho/projects/aml-discounter/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/Users/brandon/HQ/Rho/projects/aml-discounter"
    }
  }
}
```

## Conventions

- Parsers: one file per source in `app/parsers/`, each returns `list[ListEntry]`
- All XML parsing uses `lxml`. OFAC uses full-doc load + namespace strip. Others use DOM.
- Dates: normalize to ISO or year-only strings
- Names: store primary + all aliases. Use ICU for transliteration.
- Errors: never crash on bad records. Log and skip. Never return 500 to the user if Claude fails.
