# AML Discounter

Screen individuals against global sanctions and PEP lists. Discount false positives with Claude AI. Generate audit-ready XLSX reports.

## What it does

1. Matches a person's name against ~80,000 individuals across 13 authoritative public sources
2. Deduplicates cross-list matches (same person on OFAC + UN + EU + UK = 1 entry)
3. Auto-clears obvious non-matches (gender conflict, DOB gap, temporal impossibility, father's name mismatch)
4. Sends ambiguous matches to Claude Sonnet for evaluation (Pass 1)
5. Investigates remaining escalations via Perplexity web research (Pass 2)
6. Produces a 3-sheet XLSX report with per-match reasoning and citations

## Quick Start

```bash
git clone https://github.com/tmnsky/aml-discounter.git
cd aml-discounter
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env: add ANTHROPIC_API_KEY and OPENROUTER_API_KEY

python -m app.cli refresh    # fetch all sanctions data (~5-10 min first time)
uvicorn app.main:app --host 0.0.0.0 --port 8080
# Open http://localhost:8080
```

## CLI

```bash
# Screen a person
python -m app.cli screen --name "Muhammad Naeem" --dob 1994-01-20 --nationality PK -o report.xlsx

# Check data freshness
python -m app.cli status

# Refresh data
python -m app.cli refresh
```

## API

All `/api/*` endpoints require a bearer token (set via `AML_API_TOKEN` env var). Locally, if no token is set, the API is open.

```bash
# Screen a customer
curl -X POST https://aml-discounter.onrender.com/api/screen \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Muhammad Ahmed",
    "dob": "1990-05-15",
    "nationality": "PK",
    "father_name": "Abdul Rashid",
    "gender": "Male"
  }'

# Check data status
curl -H "Authorization: Bearer YOUR_TOKEN" \
  https://aml-discounter.onrender.com/api/status

# Trigger data refresh
curl -X POST -H "Authorization: Bearer YOUR_TOKEN" \
  https://aml-discounter.onrender.com/api/refresh
```

## MCP Server

Exposes screening as tools for AI agents (Claude Code, Claude Desktop).

```bash
# Run locally (stdio)
python -m app.mcp_server
```

**Claude Code config** (add to `.mcp.json`):
```json
{
  "mcpServers": {
    "aml-discounter": {
      "command": "/path/to/aml-discounter/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/path/to/aml-discounter"
    }
  }
}
```

**Tools:**
- `screen_customer` -- full screening with verdict (CLEAR / FLAG / ESCALATE)
- `get_screening_report` -- generate XLSX audit report
- `get_screening_details` -- retrieve detailed match info

## Customer Fields

| Field | Required | Purpose |
|-------|----------|---------|
| Name | Yes | Matched against all lists (fuzzy, phonetic, transliterated) |
| Date of Birth | No | DOB conflict = auto-clear if >5yr gap |
| Nationality | No | Narrows matches, flags conflicts |
| Father's Name | No | Key disambiguator for South Asian / Middle Eastern names |
| Gender | No | Gender conflict = auto-clear |
| CNIC / National ID | No | Strongest disambiguator (exact match or conflict) |
| Passport | No | Cross-referenced against listed identifiers |
| Place of Birth | No | Additional context for Claude evaluation |

## Data Sources (13)

All free, all public. No paid subscriptions required.

| Source | Records |
|--------|---------|
| OFAC SDN (US Treasury) | ~7,400 |
| OFAC Consolidated (Non-SDN) | ~100 |
| UN Security Council | ~730 |
| EU Financial Sanctions | ~4,000 |
| UK Sanctions List | ~3,800 |
| Canada SEMA | ~2,800 |
| Switzerland SECO | ~3,000 |
| Australia DFAT | ~3,800 |
| Wikidata Global PEPs | ~52,000 |
| US Congress | ~540 |
| UK Parliament | ~1,500 |
| EU Parliament | ~720 |
| FBI Most Wanted | ~1,150 |

## Screening Pipeline

```
Customer details
  → FTS5 fuzzy match + phonetic + transliteration
  → Cross-list deduplication
  → Pre-score auto-clear (DOB, gender, temporal, father's name)
  → Claude AI evaluation (Pass 1)
  → Perplexity web research (Pass 2, escalations only)
  → XLSX audit report
```

## Report Format

XLSX with 3 sheets:
- **Summary**: Verdict, subject details, source count, pipeline stats
- **Matches**: One row per unique person. Color-coded: green=cleared, red=match, yellow=escalate
- **Audit**: Full Claude prompt/response, source file hashes, timestamps

## Deployment

Deployed on Render: https://aml-discounter.onrender.com

For self-hosting, use the Dockerfile:
```bash
docker build -t aml-discounter .
docker run -p 8080:8080 -e ANTHROPIC_API_KEY=... -e AML_API_TOKEN=... aml-discounter
```

## License

MIT
