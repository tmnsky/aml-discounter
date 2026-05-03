"""MCP server exposing AML Discounter screening as tools for AI agents.

Three tools:
  screen_customer       - run full 5-stage screening, return summarized verdict
  get_screening_report  - generate XLSX audit report, return file path + URL
  get_screening_details - retrieve full match details for a past screening

Run:
  python -m app.mcp_server              # stdio (local, Claude Code/Desktop)
  python -m app.mcp_server streamable-http  # remote (deployed agent)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Annotated

import anyio
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("aml-mcp")

mcp = FastMCP(
    name="AML Discounter",
    instructions=(
        "AML/sanctions screening tool for ZAR customer support.\n\n"
        "Use `screen_customer` to check if a customer matches any sanctions or PEP lists. "
        "Screens against 11+ public databases (OFAC, UN, EU, UK, Canada, Switzerland, "
        "Australia, FBI, Wikidata PEPs, US/UK/EU parliaments).\n\n"
        "Results: CLEAR (safe), FLAG (potential match, escalate to compliance), "
        "ESCALATE (ambiguous, needs human review).\n\n"
        "After screening, use `get_screening_report` for the XLSX audit trail."
    ),
)


def _init():
    from app import db
    db.init_audit_db()


def _index_ready() -> bool:
    from app import db
    return db.INDEX_DB_PATH.exists()


def _summarize_for_agent(result: dict) -> dict:
    """Transform full screening result into a concise agent-friendly format."""
    summary = {
        "screening_id": result["id"],
        "verdict": result["result"],
        "screened_at": result["timestamp"],
        "pipeline": {
            "databases_checked": len(result.get("source_versions", {})),
            "raw_candidates": result["raw_candidates"],
            "unique_persons": result["unique_persons"],
            "auto_cleared": result["auto_cleared"],
            "auto_flagged": result["auto_flagged"],
            "ai_analyzed": result["sent_to_llm"],
            "ai_cleared": result["llm_cleared"],
            "ai_flagged": result["llm_flagged"],
            "ai_escalated": result["llm_escalated"],
            "investigations_run": result.get("investigations_run", 0),
        },
        "processing_seconds": round(result.get("processing_ms", 0) / 1000, 1),
    }

    flagged = []
    escalated = []
    for m in result.get("matches", []):
        decision = (m.get("decision") or "").upper()
        entry = {
            "matched_person": m.get("matched_person", ""),
            "source_lists": m.get("source_lists", ""),
            "designation": m.get("designation", ""),
            "reasoning": m.get("reasoning", ""),
            "investigation_sources": m.get("investigation_sources", ""),
        }
        if decision == "LIKELY_MATCH":
            flagged.append(entry)
        elif decision == "ESCALATE":
            escalated.append(entry)

    if flagged:
        summary["flagged_matches"] = flagged
    if escalated:
        summary["escalated_matches"] = escalated

    r = result
    if r["result"] == "CLEAR":
        if r["raw_candidates"] == 0:
            summary["explanation"] = (
                "No matches found in any of the screened databases. "
                "The customer does not appear on any sanctions or PEP lists."
            )
        else:
            parts = [
                f"Found {r['unique_persons']} potential name matches across "
                f"{len(r.get('source_versions', {}))} databases, "
                f"but all were determined to be false positives (different people)."
            ]
            if r["auto_cleared"]:
                parts.append(
                    f"{r['auto_cleared']} cleared by deterministic rules "
                    "(date of birth, gender, or temporal impossibility)."
                )
            if r["llm_cleared"]:
                parts.append(f"{r['llm_cleared']} cleared by AI analysis.")
            inv = r.get("investigations_run", 0)
            if inv:
                parts.append(f"{inv} resolved via web research investigation with cited sources.")
            summary["explanation"] = " ".join(parts)
    elif r["result"] == "FLAG":
        names = [m["matched_person"] for m in flagged]
        summary["explanation"] = (
            f"POTENTIAL MATCH FOUND. {len(flagged)} match(es) could not be "
            f"discounted: {', '.join(names)}. "
            "This requires review by the compliance team before proceeding."
        )
    else:
        summary["explanation"] = (
            "Screening produced ambiguous results that require human compliance review. "
            "The AI could not confidently determine whether the matches are the same person."
        )

    return summary


@mcp.tool(
    name="screen_customer",
    description=(
        "Screen a customer against 11+ sanctions and PEP databases. "
        "Returns CLEAR, FLAG, or ESCALATE verdict with reasoning. "
        "Provide as much identifying info as possible for best results. "
        "Takes 1-5 minutes depending on match complexity."
    ),
)
async def screen_customer(
    name: Annotated[str, "Full legal name of the customer (required)"],
    dob: Annotated[str, "Date of birth in YYYY-MM-DD format"] = "",
    nationality: Annotated[str, "Nationality: ISO code (PK) or full name (Pakistan)"] = "",
    cnic: Annotated[str, "CNIC or national ID number"] = "",
    passport: Annotated[str, "Passport number"] = "",
    gender: Annotated[str, "Gender: Male or Female"] = "",
    father_name: Annotated[str, "Father's name (critical for South Asian/Middle Eastern names)"] = "",
    address: Annotated[str, "Current address"] = "",
    notes: Annotated[str, "Additional context about the customer"] = "",
) -> dict:
    if not name or not name.strip():
        return {"error": "Customer name is required."}
    if not _index_ready():
        return {"error": "Sanctions data not loaded. Run 'python -m app.cli refresh' first."}

    user_input = {
        "name": name.strip(),
        "dob": dob.strip(),
        "nationality": nationality.strip(),
        "gender": gender.strip(),
        "cnic": cnic.strip(),
        "passport": passport.strip(),
        "pob": "",
        "father_name": father_name.strip(),
        "notes": f"{address}\n{notes}".strip() if address or notes else "",
    }

    from app.main import _run_screening
    from app import db

    t0 = time.time()
    result = await anyio.to_thread.run_sync(
        lambda: _run_screening(user_input, screened_by="AI Support Agent (MCP)")
    )
    result["processing_ms"] = int((time.time() - t0) * 1000)
    await anyio.to_thread.run_sync(lambda: db.save_screening(result))

    return _summarize_for_agent(result)


@mcp.tool(
    name="get_screening_report",
    description="Generate XLSX audit report for a completed screening. Returns file path and download URL.",
)
async def get_screening_report(
    screening_id: Annotated[str, "Screening ID from screen_customer (e.g. SCR-20260414-ABC123)"],
) -> dict:
    from app import db
    from app.reporter import generate_xlsx
    from app.schema import ScreeningResult

    conn = db.get_audit_conn()
    row = conn.execute("SELECT report_json FROM screenings WHERE id = ?", (screening_id,)).fetchone()
    conn.close()
    if not row:
        return {"error": f"Screening '{screening_id}' not found."}

    report_data = json.loads(row["report_json"])
    sr = ScreeningResult(**{k: v for k, v in report_data.items() if k in ScreeningResult.__dataclass_fields__})
    xlsx_bytes = await anyio.to_thread.run_sync(lambda: generate_xlsx(sr))

    output_path = db.DATA_DIR / f"{screening_id}.xlsx"
    output_path.write_bytes(xlsx_bytes)

    return {
        "screening_id": screening_id,
        "verdict": report_data.get("result", "UNKNOWN"),
        "report_path": str(output_path.resolve()),
        "report_size_kb": round(len(xlsx_bytes) / 1024, 1),
        "download_url": f"http://100.94.202.52:3040/api/screen/{screening_id}/xlsx",
        "message": f"XLSX report saved to {output_path.resolve()}. Also downloadable from the URL if web server is running.",
    }


@mcp.tool(
    name="get_screening_details",
    description="Retrieve detailed match info for a past screening. Use to explain specific matches.",
)
async def get_screening_details(
    screening_id: Annotated[str, "Screening ID from a previous screening"],
) -> dict:
    from app import db

    conn = db.get_audit_conn()
    row = conn.execute("SELECT report_json FROM screenings WHERE id = ?", (screening_id,)).fetchone()
    conn.close()
    if not row:
        return {"error": f"Screening '{screening_id}' not found."}

    report_data = json.loads(row["report_json"])
    matches = [
        {
            "number": m.get("number"),
            "decision": m.get("decision"),
            "cleared_by": m.get("cleared_by", ""),
            "matched_person": m.get("matched_person", ""),
            "aliases": m.get("aliases", ""),
            "dob": m.get("dob", ""),
            "nationality": m.get("nationality", ""),
            "gender": m.get("gender", ""),
            "designation": m.get("designation", ""),
            "source_lists": m.get("source_lists", ""),
            "key_contradiction": m.get("key_contradiction", ""),
            "reasoning": m.get("reasoning", ""),
            "investigation_sources": m.get("investigation_sources", ""),
        }
        for m in report_data.get("matches", [])
    ]

    return {
        "screening_id": screening_id,
        "verdict": report_data.get("result"),
        "customer": report_data.get("user_input", {}),
        "total_matches": len(matches),
        "matches": matches,
    }


_init()

if __name__ == "__main__":
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    mcp.run(transport=transport)
