"""CLI for AML Discounter."""

import asyncio
import json
import sys
import time

import click

from . import db


@click.group()
def cli():
    """AML False Positive Discounter — screen individuals against sanctions & PEP lists."""
    db.init_audit_db()


@cli.command()
@click.option("--name", required=True, help="Full name to screen")
@click.option("--dob", default="", help="Date of birth (YYYY-MM-DD)")
@click.option("--nationality", default="", help="Nationality (ISO code or name)")
@click.option("--gender", default="", help="Gender (Male/Female)")
@click.option("--cnic", default="", help="CNIC or national ID number")
@click.option("--passport", default="", help="Passport number")
@click.option("--pob", default="", help="Place of birth")
@click.option("--screened-by", default="", help="Name of person running the screening")
@click.option("--output", "-o", default=None, help="Output file path (.xlsx or .json)")
def screen(name, dob, nationality, gender, cnic, passport, pob, screened_by, output):
    """Screen an individual against sanctions and PEP lists."""
    if not db.INDEX_DB_PATH.exists():
        click.echo("Error: Data not loaded. Run 'aml-screen refresh' first.", err=True)
        sys.exit(1)

    user_input = {
        "name": name,
        "dob": dob,
        "nationality": nationality,
        "gender": gender,
        "cnic": cnic,
        "passport": passport,
        "pob": pob,
        "notes": "",
    }

    click.echo(f"Screening: {name}")
    t0 = time.time()

    from .main import _run_screening
    result = _run_screening(user_input, screened_by)
    result["processing_ms"] = int((time.time() - t0) * 1000)

    db.save_screening(result)

    # Print summary
    click.echo(f"\nResult: {result['result']}")
    click.echo(f"  Raw candidates: {result['raw_candidates']}")
    click.echo(f"  Unique persons: {result['unique_persons']}")
    click.echo(f"  Auto-cleared: {result['auto_cleared']}")
    click.echo(f"  AI analyzed: {result['sent_to_llm']}")
    click.echo(f"  Flagged: {result['llm_flagged']}")
    click.echo(f"  Escalated: {result['llm_escalated']}")
    click.echo(f"  Processing: {result['processing_ms']}ms")
    click.echo(f"  Report ID: {result['id']}")

    if output:
        if output.endswith(".xlsx"):
            from .reporter import generate_xlsx
            from .schema import ScreeningResult
            sr = ScreeningResult(**{k: v for k, v in result.items() if k in ScreeningResult.__dataclass_fields__})
            with open(output, "wb") as f:
                f.write(generate_xlsx(sr))
            click.echo(f"\nXLSX saved: {output}")
        elif output.endswith(".json"):
            with open(output, "w") as f:
                json.dump(result, f, indent=2)
            click.echo(f"\nJSON saved: {output}")
        else:
            click.echo(f"Unknown output format: {output}. Use .xlsx or .json", err=True)


@cli.command()
def refresh():
    """Fetch/update all sanctions and PEP list data."""
    click.echo("Refreshing all data sources...")

    async def _do_refresh():
        from .fetcher import refresh_all_sources
        await refresh_all_sources()

    asyncio.run(_do_refresh())
    click.echo("Done.")


@cli.command()
def status():
    """Show data freshness and source status."""
    conn = db.get_audit_conn()
    rows = conn.execute("SELECT * FROM source_metadata ORDER BY source").fetchall()
    conn.close()

    if not rows:
        click.echo("No data loaded. Run 'aml-screen refresh' first.")
        return

    total = 0
    click.echo(f"{'Source':<25} {'Entities':>10} {'Last Fetched':<25} {'Status':<10}")
    click.echo("-" * 75)
    for r in rows:
        total += r["entity_count"] or 0
        click.echo(f"{r['source']:<25} {r['entity_count'] or 0:>10} {r['last_fetched'] or 'Never':<25} {r['status']:<10}")
    click.echo("-" * 75)
    click.echo(f"{'TOTAL':<25} {total:>10}")

    click.echo(f"\nIndex exists: {db.INDEX_DB_PATH.exists()}")


@cli.command()
@click.option("--input", "-i", "input_file", required=True, help="JSON file with user details")
@click.option("--output", "-o", required=True, help="Output file (.xlsx or .json)")
@click.option("--screened-by", default="", help="Name of person running the screening")
def screen_file(input_file, output, screened_by):
    """Screen a user from a JSON file."""
    with open(input_file) as f:
        user_input = json.load(f)

    if "name" not in user_input:
        click.echo("Error: JSON must contain 'name' field", err=True)
        sys.exit(1)

    # Reuse the screen command logic
    from .main import _run_screening
    t0 = time.time()
    result = _run_screening(user_input, screened_by)
    result["processing_ms"] = int((time.time() - t0) * 1000)
    db.save_screening(result)

    if output.endswith(".xlsx"):
        from .reporter import generate_xlsx
        from .schema import ScreeningResult
        sr = ScreeningResult(**{k: v for k, v in result.items() if k in ScreeningResult.__dataclass_fields__})
        with open(output, "wb") as f:
            f.write(generate_xlsx(sr))
    else:
        with open(output, "w") as f:
            json.dump(result, f, indent=2)

    click.echo(f"Result: {result['result']} → {output}")


def main():
    cli()


if __name__ == "__main__":
    main()
