# AML Discounter

Screen individuals against global sanctions and PEP lists. Discount false positives with Claude AI. Generate audit-ready XLSX reports.

## What it does

1. Matches a person's name against ~25,000 individuals across 11 authoritative public sources
2. Deduplicates cross-list matches (same person on OFAC + UN + EU + UK = 1 entry)
3. Auto-clears obvious non-matches (gender conflict, DOB >10yr gap, different IDs)
4. Sends ambiguous matches to Claude Sonnet for evaluation
5. Produces a 3-sheet XLSX report with per-match reasoning

## Quick Start

```bash
# Clone and setup
git clone https://github.com/zarpay/aml-discounter.git
cd aml-discounter
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY

# Fetch all sanctions data (~5-10 min first time)
python -m app.cli refresh

# Start web UI
uvicorn app.main:app --host 0.0.0.0 --port 8080
# Open http://localhost:8080
```

## Docker

```bash
docker run -p 8080:8080 -e ANTHROPIC_API_KEY=sk-ant-... ghcr.io/zarpay/aml-discounter:latest
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

## Data Sources (11)

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

## Report Format

XLSX with 3 sheets:
- **Summary**: Verdict, subject details, source count, pipeline stats
- **Matches**: One row per unique person. Color-coded: green=cleared, red=match, yellow=escalate
- **Audit**: Full Claude prompt/response, source file hashes, timestamps

## How It Works

```
User details → FTS5 fuzzy match → Cross-list dedup → Pre-score filter → Claude AI → XLSX Report
                  80 candidates     30 unique          12 to AI           All reasoned
```

## License

MIT
