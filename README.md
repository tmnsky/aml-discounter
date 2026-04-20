# AML Discounter

Screen individuals against global sanctions and PEP lists. Discount false positives with Claude AI. Generate audit-ready XLSX reports.

## What it does

1. Matches a person's name against ~80,000 individuals across 13 authoritative public sources
2. Deduplicates cross-list matches (same person on OFAC + UN + EU + UK = 1 entry)
3. Auto-clears obvious non-matches (gender conflict, DOB gap, temporal impossibility)
4. Sends ambiguous matches to Claude Sonnet for evaluation (Pass 1)
5. Investigates remaining escalations via Perplexity web research (Pass 2)
6. Produces a 3-sheet XLSX report with per-match reasoning and citations

## Quick Start (Local)

```bash
git clone https://github.com/zarpay/aml-discounter.git
cd aml-discounter
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env: add ANTHROPIC_API_KEY and OPENROUTER_API_KEY

python -m app.cli refresh    # fetch all sanctions data (~5-10 min first time)
uvicorn app.main:app --host 0.0.0.0 --port 3040
```

## Docker

```bash
docker run -p 3040:3040 -v aml-data:/data \
  -e ANTHROPIC_API_KEY=... \
  -e OPENROUTER_API_KEY=... \
  ghcr.io/zarpay/aml-discounter:latest
```

## Deploy to Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/zarpay/aml-discounter)

Or use the `render.yaml` blueprint. Set environment variables in the Render dashboard:
- `ANTHROPIC_API_KEY` — Claude API key
- `OPENROUTER_API_KEY` — OpenRouter key (for Perplexity research)
- `AML_API_TOKEN` — bearer token for API auth (auto-generated)

The Render service includes a 10GB persistent disk at `/data` for the sanctions index and audit logs. Data refreshes automatically on startup if stale.

## API Authentication

Set `AML_API_TOKEN` to protect the API in production. All `/api/*` routes require `Authorization: Bearer <token>`.

```bash
# Screen a customer (authenticated)
curl -X POST https://aml-discounter.onrender.com/api/screen \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "SALMAN AMIN", "dob": "1994-10-16", "nationality": "Pakistan", "cnic": "4200008430555"}'
```

Locally, if `AML_API_TOKEN` is not set, auth is skipped (open access for development).

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Web UI (no auth required) |
| POST | `/api/screen` | Screen a customer, return JSON result |
| GET | `/api/screen/{id}` | Retrieve a past screening |
| GET | `/api/screen/{id}/xlsx` | Download XLSX report |
| GET | `/api/status` | Data freshness per source |
| GET | `/api/history` | List past screenings |
| POST | `/api/refresh` | Trigger data refresh |

## MCP Server (Local)

For Claude Code / Claude Desktop, the tool also runs as an MCP server:

```bash
python -m app.mcp_server  # stdio transport
```

Tools: `screen_customer`, `get_screening_report`, `get_screening_details`

## CLI

```bash
python -m app.cli screen --name "Muhammad Naeem" --dob 1994-01-20 --nationality PK -o report.xlsx
python -m app.cli status
python -m app.cli refresh
```

## How It Works

```
Customer details
  |
Stage 1: FTS5 fuzzy match        → ~200 raw candidates
Stage 2: Cross-list dedup         → ~120 unique persons
Stage 3: Pre-score (rules)        → ~50 auto-cleared
Stage 4: Claude Pass 1 (batched)  → ~65 AI-cleared, ~5 escalated
Stage 5: Pass 2 investigation     → Perplexity + Claude resolves rest
  |
XLSX report with per-match reasoning + citations
```

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
| Wikidata Global PEPs | ~288,000 |
| US Congress | 538 |
| UK Parliament | 5,227 |
| EU Parliament | 718 |
| FBI Most Wanted | 1,151 |
| Australia DFAT | ~3,800 |

## Report Format

XLSX with 3 sheets:
- **Summary**: Verdict, customer details, source count, pipeline stats, investigations
- **Matches**: One row per unique person. Color-coded: green=cleared, red=match, yellow=escalate. Includes investigation sources (citations).
- **Audit**: Full Claude prompt/response, Perplexity Q&A, source file hashes

## License

MIT
