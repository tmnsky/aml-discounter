"""Tests for the MCP server summary function and tool logic."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

# Ensure DATA_DIR exists before module-level _init() runs
os.makedirs("data", exist_ok=True)

from app.mcp_server import _summarize_for_agent


# ---------------------------------------------------------------------------
# _summarize_for_agent
# ---------------------------------------------------------------------------


def _make_result(**overrides) -> dict:
    defaults = {
        "id": "SCR-TEST-001",
        "timestamp": "2026-04-19T10:00:00",
        "user_input": {"name": "Test User", "dob": "2003-08-31"},
        "result": "CLEAR",
        "raw_candidates": 80,
        "unique_persons": 30,
        "auto_cleared": 20,
        "auto_flagged": 0,
        "sent_to_llm": 10,
        "llm_cleared": 10,
        "llm_flagged": 0,
        "llm_escalated": 0,
        "investigations_run": 0,
        "matches": [],
        "llm_calls": [],
        "investigation_audits": [],
        "source_versions": {"ofac_sdn": "abc", "un_consolidated": "def"},
        "processing_ms": 5000,
        "screened_by": "test",
    }
    defaults.update(overrides)
    return defaults


def test_summarize_clear_no_candidates():
    result = _make_result(raw_candidates=0, unique_persons=0, auto_cleared=0, sent_to_llm=0)
    summary = _summarize_for_agent(result)

    assert summary["verdict"] == "CLEAR"
    assert summary["screening_id"] == "SCR-TEST-001"
    assert "No matches found" in summary["explanation"]
    assert "flagged_matches" not in summary
    assert "escalated_matches" not in summary


def test_summarize_clear_with_discounted_matches():
    result = _make_result(
        raw_candidates=80,
        unique_persons=30,
        auto_cleared=20,
        sent_to_llm=10,
        llm_cleared=10,
        investigations_run=2,
    )
    summary = _summarize_for_agent(result)

    assert summary["verdict"] == "CLEAR"
    assert "30 potential name matches" in summary["explanation"]
    assert "false positives" in summary["explanation"]
    assert "20 cleared by deterministic rules" in summary["explanation"]
    assert "10 cleared by AI" in summary["explanation"]
    assert "2 resolved via web research" in summary["explanation"]
    assert summary["pipeline"]["databases_checked"] == 2
    assert summary["processing_seconds"] == 5.0


def test_summarize_flag():
    matches = [
        {
            "decision": "LIKELY_MATCH",
            "matched_person": "Hafiz Saeed",
            "source_lists": "UN, OFAC",
            "designation": "Taliban",
            "reasoning": "Name and nationality match",
        },
        {
            "decision": "CLEARED",
            "matched_person": "Some Other Person",
            "reasoning": "Different DOB",
        },
    ]
    result = _make_result(result="FLAG", llm_flagged=1, matches=matches)
    summary = _summarize_for_agent(result)

    assert summary["verdict"] == "FLAG"
    assert len(summary["flagged_matches"]) == 1
    assert summary["flagged_matches"][0]["matched_person"] == "Hafiz Saeed"
    assert "POTENTIAL MATCH" in summary["explanation"]
    assert "Hafiz Saeed" in summary["explanation"]
    assert "escalated_matches" not in summary
    # Cleared matches should NOT be in the summary
    assert not any(m.get("matched_person") == "Some Other Person" for m in summary.get("flagged_matches", []))


def test_summarize_escalate():
    matches = [
        {
            "decision": "ESCALATE",
            "matched_person": "Ambiguous Person",
            "source_lists": "Wikidata PEPs",
            "reasoning": "Cannot determine",
        },
    ]
    result = _make_result(result="ESCALATE", llm_escalated=1, matches=matches)
    summary = _summarize_for_agent(result)

    assert summary["verdict"] == "ESCALATE"
    assert len(summary["escalated_matches"]) == 1
    assert "human compliance review" in summary["explanation"]


def test_summarize_pipeline_stats():
    result = _make_result(
        raw_candidates=200,
        unique_persons=119,
        auto_cleared=46,
        auto_flagged=0,
        sent_to_llm=73,
        llm_cleared=73,
        llm_flagged=0,
        llm_escalated=0,
        investigations_run=5,
    )
    summary = _summarize_for_agent(result)

    p = summary["pipeline"]
    assert p["raw_candidates"] == 200
    assert p["unique_persons"] == 119
    assert p["auto_cleared"] == 46
    assert p["ai_analyzed"] == 73
    assert p["ai_cleared"] == 73
    assert p["investigations_run"] == 5


def test_summarize_investigation_sources_in_flagged():
    matches = [
        {
            "decision": "LIKELY_MATCH",
            "matched_person": "Someone",
            "source_lists": "OFAC",
            "designation": "",
            "reasoning": "Match found",
            "investigation_sources": "https://example.com",
        },
    ]
    result = _make_result(result="FLAG", llm_flagged=1, matches=matches)
    summary = _summarize_for_agent(result)

    assert summary["flagged_matches"][0]["investigation_sources"] == "https://example.com"


def test_summarize_salman_amin_case():
    """Simulate the Salman Amin case Brandon described."""
    result = _make_result(
        id="SCR-20260419-SALMAN",
        user_input={
            "name": "SALMAN",
            "dob": "1994-10-16",
            "nationality": "Pakistan",
            "cnic": "4200008430555",
        },
        result="CLEAR",
        raw_candidates=15,
        unique_persons=8,
        auto_cleared=6,
        auto_flagged=0,
        sent_to_llm=2,
        llm_cleared=2,
        llm_flagged=0,
        llm_escalated=0,
        investigations_run=0,
    )
    summary = _summarize_for_agent(result)

    assert summary["verdict"] == "CLEAR"
    assert summary["screening_id"] == "SCR-20260419-SALMAN"
    assert "8 potential name matches" in summary["explanation"]
    assert "false positives" in summary["explanation"]
