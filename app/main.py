"""FastAPI app for AML False Positive Discounter."""

import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from . import db

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("aml-discounter")

app = FastAPI(title="AML Discounter", version="1.0.0")

# Serve static files
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Mount MCP server at /mcp for remote clients
try:
    from .mcp_server import mcp as mcp_server
    mcp_app = mcp_server.streamable_http_app()
    app.mount("/mcp", mcp_app)
    logger.info("MCP server mounted at /mcp")
except Exception as e:
    logger.warning("Could not mount MCP server: %s", e)

# Initialize audit DB on startup
db.init_audit_db()


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the web UI."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text())
    return HTMLResponse(content="<h1>AML Discounter</h1><p>Static files not found.</p>")


@app.get("/api/status")
async def status():
    """Data freshness per source, record counts, last refresh time."""
    conn = db.get_audit_conn()
    rows = conn.execute("SELECT * FROM source_metadata ORDER BY source").fetchall()
    conn.close()

    sources = [dict(r) for r in rows]
    total_entities = sum(s.get("entity_count", 0) for s in sources)
    index_exists = db.INDEX_DB_PATH.exists()

    # Fallback: if metadata is empty but the index exists, count directly
    if index_exists and total_entities == 0:
        try:
            idx_conn = db.get_index_conn()
            total_entities = idx_conn.execute("SELECT COUNT(*) FROM sanctions_entities").fetchone()[0]
            idx_conn.close()
        except Exception:
            pass

    return {
        "ready": index_exists and total_entities > 0,
        "total_entities": total_entities,
        "sources": sources,
        "index_path": str(db.INDEX_DB_PATH),
        "index_exists": index_exists,
    }


@app.post("/api/screen")
async def screen(request: Request):
    """Run a full screening and return JSON report."""
    body = await request.json()
    user_input = {
        "name": body.get("name", "").strip(),
        "dob": body.get("dob", "").strip(),
        "nationality": body.get("nationality", "").strip(),
        "gender": body.get("gender", "").strip(),
        "cnic": body.get("cnic", "").strip(),
        "passport": body.get("passport", "").strip(),
        "pob": body.get("pob", "").strip(),
        "father_name": body.get("father_name", "").strip(),
        "notes": body.get("notes", "").strip(),
    }
    screened_by = body.get("screened_by", "").strip()

    if not user_input["name"]:
        raise HTTPException(status_code=400, detail="Name is required")

    if not db.INDEX_DB_PATH.exists():
        raise HTTPException(status_code=503, detail="Data not loaded yet. Run a refresh first.")

    t0 = time.time()
    result = _run_screening(user_input, screened_by)
    result["processing_ms"] = int((time.time() - t0) * 1000)

    # Save to audit log
    db.save_screening(result)

    return result


@app.get("/api/screen/{screening_id}")
async def get_screening(screening_id: str):
    """Retrieve a past screening by ID."""
    conn = db.get_audit_conn()
    row = conn.execute("SELECT * FROM screenings WHERE id = ?", (screening_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Screening not found")
    return json.loads(dict(row)["report_json"])


@app.get("/api/screen/{screening_id}/xlsx")
async def get_screening_xlsx(screening_id: str):
    """Download XLSX report for a past screening."""
    conn = db.get_audit_conn()
    row = conn.execute("SELECT report_json FROM screenings WHERE id = ?", (screening_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Screening not found")

    from .reporter import generate_xlsx
    from .schema import ScreeningResult

    report_data = json.loads(row["report_json"])
    result = ScreeningResult(**{k: v for k, v in report_data.items() if k in ScreeningResult.__dataclass_fields__})

    xlsx_bytes = generate_xlsx(result)
    tmp_path = db.DATA_DIR / f"{screening_id}.xlsx"
    tmp_path.write_bytes(xlsx_bytes)

    return FileResponse(
        str(tmp_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{screening_id}.xlsx",
    )


@app.get("/api/history")
async def history(limit: int = 50, offset: int = 0):
    """List past screenings."""
    conn = db.get_audit_conn()
    rows = conn.execute(
        "SELECT id, created_at, user_input, result, raw_candidates, unique_persons, processing_ms, screened_by FROM screenings ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM screenings").fetchone()[0]
    conn.close()

    return {
        "total": total,
        "screenings": [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "name": json.loads(r["user_input"]).get("name", ""),
                "result": r["result"],
                "raw_candidates": r["raw_candidates"],
                "unique_persons": r["unique_persons"],
                "processing_ms": r["processing_ms"],
                "screened_by": r["screened_by"],
            }
            for r in rows
        ],
    }


@app.post("/api/refresh")
async def refresh(background_tasks: BackgroundTasks):
    """Trigger data refresh."""
    background_tasks.add_task(_run_refresh)
    return {"status": "refresh_started"}


def _run_screening(user_input: dict, screened_by: str = "") -> dict:
    """Execute the full screening pipeline."""
    from .matcher import find_candidates, transliterate
    from .dedup import dedup_candidates
    from .prescore import prescore
    from .discounter import discount_matches
    from .schema import ScreeningResult

    screening_id = f"SCR-{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

    # Get source versions for audit
    conn_audit = db.get_audit_conn()
    source_rows = conn_audit.execute("SELECT source, file_hash FROM source_metadata WHERE status = 'ok'").fetchall()
    conn_audit.close()
    source_versions = {r["source"]: r["file_hash"] for r in source_rows}

    # Build identifiers for prescore from cnic/passport fields
    user_identifiers = []
    if user_input.get("cnic"):
        user_identifiers.append({"type": "cnic", "value": user_input["cnic"]})
    if user_input.get("passport"):
        user_identifiers.append({"type": "passport", "value": user_input["passport"]})
    user_for_prescore = {**user_input, "identifiers": user_identifiers}

    # Stage 1: Find candidates
    conn = db.get_index_conn()
    try:
        raw_candidates = find_candidates(user_input["name"], conn)
    finally:
        conn.close()

    # Stage 2: Dedup
    deduped = dedup_candidates(raw_candidates)

    # Stage 3: Pre-score
    auto_cleared, auto_flagged, send_to_llm = prescore(user_for_prescore, deduped)

    # Extract DeduplicatedMatch objects from prescore dicts for the discounter
    matches_for_llm = [item["match"] for item in send_to_llm]

    # Stage 4: Claude discounting (Pass 1)
    llm_decisions, llm_calls = [], []
    if matches_for_llm:
        llm_decisions, llm_calls = discount_matches(user_input, matches_for_llm)

    # Stage 5: Pass 2 investigation on escalations
    # For each ESCALATE result, use Perplexity web research + Claude reasoning to try to resolve
    investigation_audits: list[dict] = []
    investigated_indices: dict[int, object] = {}  # map from send_to_llm index → new MatchDecision
    if llm_decisions:
        from .investigator import investigate_escalations

        escalations_to_investigate = []
        for i, decision in enumerate(llm_decisions):
            if decision.decision == "ESCALATE":
                # Use the send_to_llm index (0-based) to find the match
                match = send_to_llm[i]["match"]
                escalations_to_investigate.append((i, match))

        if escalations_to_investigate:
            new_decisions, investigation_audits = investigate_escalations(
                user_input, escalations_to_investigate
            )
            # Map back: replace original decision with new one
            for (orig_idx, _), new_decision in zip(escalations_to_investigate, new_decisions):
                # Preserve contradictions from investigation
                investigated_indices[orig_idx] = new_decision

    # Build matches list for report
    all_matches = []
    match_num = 0

    for entry in auto_cleared:
        item = entry["match"]
        reason = entry["reason"]
        match_num += 1
        rep = item.representative
        all_matches.append({
            "number": match_num,
            "decision": "CLEARED",
            "confidence": "",
            "cleared_by": f"Rule: {reason}",
            "matched_person": rep.names[0] if rep.names else "",
            "aliases": ", ".join(item.all_names[1:5]) if len(item.all_names) > 1 else "",
            "dob": ", ".join(rep.dob) if rep.dob else "",
            "nationality": ", ".join(rep.nationality) if rep.nationality else "",
            "gender": rep.gender or "",
            "designation": rep.designation or "",
            "source_lists": ", ".join(s["list_name"] for s in item.all_sources) if item.all_sources else rep.list_name,
            "identifiers": "; ".join(f"{d['type']}: {d['value']}" for d in item.all_identifiers[:3]) if item.all_identifiers else "",
            "key_contradiction": reason,
            "reasoning": f"Auto-cleared: {reason}",
        })

    for entry in auto_flagged:
        item = entry["match"]
        reason = entry["reason"]
        match_num += 1
        rep = item.representative
        all_matches.append({
            "number": match_num,
            "decision": "LIKELY_MATCH",
            "confidence": "1.0",
            "cleared_by": f"Rule: {reason}",
            "matched_person": rep.names[0] if rep.names else "",
            "aliases": ", ".join(item.all_names[1:5]) if len(item.all_names) > 1 else "",
            "dob": ", ".join(rep.dob) if rep.dob else "",
            "nationality": ", ".join(rep.nationality) if rep.nationality else "",
            "gender": rep.gender or "",
            "designation": rep.designation or "",
            "source_lists": ", ".join(s["list_name"] for s in item.all_sources) if item.all_sources else rep.list_name,
            "identifiers": "; ".join(f"{d['type']}: {d['value']}" for d in item.all_identifiers[:3]) if item.all_identifiers else "",
            "key_contradiction": "",
            "reasoning": f"Auto-flagged: {reason}",
        })

    for i, (entry, decision) in enumerate(zip(send_to_llm, llm_decisions)):
        item = entry["match"]
        match_num += 1
        rep = item.representative

        # If this escalation was investigated in Pass 2, use the new decision
        investigated_decision = investigated_indices.get(i)
        if investigated_decision is not None:
            final_decision_obj = investigated_decision
            cleared_by = "AI + Investigation"
        else:
            final_decision_obj = decision
            cleared_by = "AI"

        contras = "; ".join(
            d.get("detail", str(d)) for d in final_decision_obj.contradictions
        ) if final_decision_obj.contradictions else ""

        # Find matching investigation audit record for citations
        investigation_sources = ""
        for audit_rec in investigation_audits:
            if audit_rec.get("match_number") == i:
                cites = audit_rec.get("perplexity_citations", [])
                if cites:
                    investigation_sources = "; ".join(
                        c.get("url", "") for c in cites[:3] if c.get("url")
                    )
                break

        all_matches.append({
            "number": match_num,
            "decision": final_decision_obj.decision,
            "confidence": "",
            "cleared_by": cleared_by,
            "matched_person": rep.names[0] if rep.names else "",
            "aliases": ", ".join(item.all_names[1:5]) if len(item.all_names) > 1 else "",
            "dob": ", ".join(rep.dob) if rep.dob else "",
            "nationality": ", ".join(rep.nationality) if rep.nationality else "",
            "gender": rep.gender or "",
            "designation": rep.designation or "",
            "source_lists": ", ".join(s["list_name"] for s in item.all_sources) if item.all_sources else rep.list_name,
            "identifiers": "; ".join(f"{d['type']}: {d['value']}" for d in item.all_identifiers[:3]) if item.all_identifiers else "",
            "key_contradiction": contras,
            "reasoning": final_decision_obj.reasoning,
            "investigation_sources": investigation_sources,
        })

    # Build the effective final decisions (Pass 2 overrides Pass 1 for investigated items)
    final_decisions = []
    for i, decision in enumerate(llm_decisions):
        investigated = investigated_indices.get(i)
        final_decisions.append(investigated if investigated is not None else decision)

    # Determine overall result (using final decisions after investigation)
    llm_cleared_count = sum(1 for d in final_decisions if d.decision == "CLEARED")
    llm_flagged_count = sum(1 for d in final_decisions if d.decision == "LIKELY_MATCH")
    llm_escalated_count = sum(1 for d in final_decisions if d.decision == "ESCALATE")

    if auto_flagged or llm_flagged_count > 0:
        overall_result = "FLAG"
    elif llm_escalated_count > 0:
        overall_result = "ESCALATE"
    else:
        overall_result = "CLEAR"

    return {
        "id": screening_id,
        "timestamp": datetime.utcnow().isoformat(),
        "user_input": user_input,
        "result": overall_result,
        "raw_candidates": len(raw_candidates),
        "unique_persons": len(deduped),
        "auto_cleared": len(auto_cleared),
        "auto_flagged": len(auto_flagged),
        "sent_to_llm": len(send_to_llm),
        "llm_cleared": llm_cleared_count,
        "llm_flagged": llm_flagged_count,
        "llm_escalated": llm_escalated_count,
        "investigations_run": len(investigation_audits),
        "matches": all_matches,
        "llm_calls": llm_calls,
        "investigation_audits": investigation_audits,
        "source_versions": source_versions,
        "processing_ms": 0,
        "screened_by": screened_by,
    }


async def _run_refresh():
    """Background task to refresh all data sources."""
    logger.info("Starting data refresh...")
    try:
        from .fetcher import refresh_all_sources
        import asyncio
        await refresh_all_sources()
        logger.info("Data refresh completed successfully.")
    except Exception as e:
        logger.error(f"Data refresh failed: {e}")
