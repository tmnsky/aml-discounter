"""Unit tests for the discounter safety guards.

Two fail-safe guards prevent the most dangerous failure mode: clearing a
candidate that should have been escalated.

  Guard 1 (_parse_response): if Claude returns a CLEARED verdict but its own
    reasoning contains escalation/uncertainty language, force ESCALATE.

  Guard 2 (_apply_no_data_guard): a CLEARED verdict on a sanctions/wanted
    listing that has no DOB, nationality, or ID rests on name analysis alone.
    Force ESCALATE — you cannot confidently clear against a ghost.
"""

from __future__ import annotations

import json

from app.discounter import (
    _parse_response,
    _apply_no_data_guard,
    _on_hard_list,
    _has_no_biographical_data,
)
from app.schema import DeduplicatedMatch, ListEntry


def make_match(
    source: str = "ofac_sdn",
    list_name: str = "OFAC SDN List",
    dob: list[str] | None = None,
    nationality: list[str] | None = None,
    identifiers: list[dict] | None = None,
) -> DeduplicatedMatch:
    return DeduplicatedMatch(
        representative=ListEntry(
            id=f"{source}-1",
            source=source,
            list_name=list_name,
            names=["Test Candidate"],
            dob=dob or [],
            nationality=nationality or [],
        ),
        all_sources=[{"source": source, "list_name": list_name}],
        all_names=["Test Candidate"],
        all_identifiers=identifiers or [],
    )


# ---------------------------------------------------------------------------
# Guard 1: verdict / reasoning contradiction
# ---------------------------------------------------------------------------


def test_cleared_with_escalate_in_reasoning_is_downgraded():
    """The real match [63] regression: CLEARED but reasoning says ESCALATE."""
    resp = json.dumps([{
        "match_number": 1,
        "verdict": "CLEARED",
        "contradictions": [],
        "supporting_similarities": [],
        "reasoning": "Khawaja is a distinct title; ESCALATE is warranted given the alias overlap.",
    }])
    parsed = _parse_response(resp, 1)
    assert parsed[0]["verdict"] == "ESCALATE"


def test_clean_clear_is_preserved():
    """A confident DOB-based clear with no doubt language stays CLEARED."""
    resp = json.dumps([{
        "match_number": 1,
        "verdict": "CLEARED",
        "contradictions": ["DOB: customer 1998 vs candidate 1952 (46yr gap)"],
        "supporting_similarities": [],
        "reasoning": "Customer born 1998, candidate born 1952. Clearly a different person.",
    }])
    parsed = _parse_response(resp, 1)
    assert parsed[0]["verdict"] == "CLEARED"


def test_human_review_language_downgrades():
    resp = json.dumps([{
        "match_number": 1, "verdict": "CLEARED", "contradictions": [],
        "supporting_similarities": [],
        "reasoning": "Names are similar; this warrants human review before clearing.",
    }])
    assert _parse_response(resp, 1)[0]["verdict"] == "ESCALATE"


def test_escalation_language_does_not_affect_likely_match():
    """Guard only rewrites CLEARED verdicts, not LIKELY_MATCH/ESCALATE."""
    resp = json.dumps([{
        "match_number": 1, "verdict": "LIKELY_MATCH", "contradictions": [],
        "supporting_similarities": [], "reasoning": "Strong match, escalate to compliance.",
    }])
    assert _parse_response(resp, 1)[0]["verdict"] == "LIKELY_MATCH"


# ---------------------------------------------------------------------------
# Guard 2: no-data clearing on hard lists
# ---------------------------------------------------------------------------


def test_ghost_on_sanctions_list_is_escalated():
    """No DOB, no nationality, no ID on OFAC SDN -> cannot clear on name alone."""
    match = make_match(source="ofac_sdn")
    verdict, reasoning = _apply_no_data_guard("CLEARED", "Name structure differs.", match)
    assert verdict == "ESCALATE"
    assert "Auto-escalated" in reasoning


def test_sanctions_with_dob_can_clear():
    match = make_match(source="ofac_sdn", dob=["1952"])
    verdict, _ = _apply_no_data_guard("CLEARED", "46yr DOB gap.", match)
    assert verdict == "CLEARED"


def test_sanctions_with_nationality_can_clear():
    match = make_match(source="ofac_sdn", nationality=["IR"])
    verdict, _ = _apply_no_data_guard("CLEARED", "Different nationality.", match)
    assert verdict == "CLEARED"


def test_sanctions_with_identifier_can_clear():
    match = make_match(source="ofac_sdn", identifiers=[{"type": "passport", "value": "X123"}])
    verdict, _ = _apply_no_data_guard("CLEARED", "Different passport.", match)
    assert verdict == "CLEARED"


def test_pep_with_no_data_can_still_clear():
    """PEP listings are lower risk than sanctions — the guard does not apply."""
    match = make_match(source="wikidata_peps", list_name="Wikidata PEPs (PK)")
    verdict, _ = _apply_no_data_guard("CLEARED", "Different surname.", match)
    assert verdict == "CLEARED"


def test_guard_does_not_touch_non_cleared_verdicts():
    match = make_match(source="ofac_sdn")
    verdict, _ = _apply_no_data_guard("LIKELY_MATCH", "Match.", match)
    assert verdict == "LIKELY_MATCH"


def test_fbi_wanted_is_a_hard_list():
    assert _on_hard_list(make_match(source="fbi_wanted", list_name="FBI Most Wanted"))


def test_parliament_is_not_a_hard_list():
    assert not _on_hard_list(make_match(source="uk_parliament", list_name="UK Parliament"))


def test_no_biographical_data_detection():
    assert _has_no_biographical_data(make_match())
    assert not _has_no_biographical_data(make_match(dob=["1990"]))
    assert not _has_no_biographical_data(
        make_match(identifiers=[{"type": "cnic", "value": "123"}])
    )
