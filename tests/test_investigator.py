"""Unit tests for the Pass 2 investigator module."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.investigator import (
    _build_question,
    _parse_claude_json,
    _call_perplexity,
    _reason_with_claude,
    investigate,
    investigate_escalations,
)
from app.schema import DeduplicatedMatch, ListEntry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_match(
    name: str = "Test Person",
    source: str = "un_consolidated",
    list_name: str = "UN Security Council",
    dob: list[str] | None = None,
    nationality: list[str] | None = None,
    designation: str | None = None,
    listed_on: str | None = None,
    programs: list[str] | None = None,
) -> DeduplicatedMatch:
    rep = ListEntry(
        id=f"test-{name}",
        source=source,
        list_name=list_name,
        names=[name],
        dob=dob or [],
        nationality=nationality or [],
        designation=designation,
        listed_on=listed_on,
        programs=programs or [],
    )
    return DeduplicatedMatch(
        representative=rep,
        all_sources=[{
            "source": source,
            "list_name": list_name,
            "source_url": "",
            "listed_on": listed_on,
            "programs": programs or [],
        }],
        all_names=[name],
        all_identifiers=[],
    )


# ---------------------------------------------------------------------------
# _build_question
# ---------------------------------------------------------------------------


def test_build_question_pep():
    customer = {"name": "Abdul Manan", "dob": "2003-08-31", "nationality": "Pakistan"}
    match = make_match(
        name="Mian Abdul Manan",
        source="wikidata_peps",
        list_name="Wikidata PEPs (PK)",
        nationality=["Pakistan"],
        designation="Member of National Assembly",
    )
    q = _build_question(customer, match)
    assert "Mian Abdul Manan" in q
    assert "Pakistan" in q
    assert "political role" in q.lower() or "years served" in q.lower()


def test_build_question_sanctions():
    customer = {"name": "Abdul Manan", "dob": "2003-08-31"}
    match = make_match(
        name="Abdul Manan Agha",
        source="un_consolidated",
        list_name="UN Security Council",
        listed_on="2001-10-17",
        programs=["Al-Qaida"],
    )
    q = _build_question(customer, match)
    assert "Abdul Manan Agha" in q
    assert "2001-10-17" in q
    assert "Al-Qaida" in q
    # Sanctions questions should ask about listing history
    assert "sanctioned" in q.lower() or "listed" in q.lower()


# ---------------------------------------------------------------------------
# _parse_claude_json
# ---------------------------------------------------------------------------


def test_parse_claude_json_clean():
    text = json.dumps({
        "decision": "CLEARED",
        "contradictions": ["DOB: research says 1957, customer says 2003"],
        "supporting_findings": [],
        "reasoning": "Customer is too young.",
    })
    parsed = _parse_claude_json(text)
    assert parsed["decision"] == "CLEARED"
    assert parsed["contradictions"] == ["DOB: research says 1957, customer says 2003"]
    assert parsed["reasoning"] == "Customer is too young."


def test_parse_claude_json_with_markdown_fence():
    text = "```json\n" + json.dumps({"decision": "LIKELY_MATCH", "reasoning": "match"}) + "\n```"
    parsed = _parse_claude_json(text)
    assert parsed["decision"] == "LIKELY_MATCH"


def test_parse_claude_json_invalid_verdict_falls_to_escalate():
    text = json.dumps({"decision": "FOOBAR", "reasoning": "?"})
    parsed = _parse_claude_json(text)
    assert parsed["decision"] == "ESCALATE"


def test_parse_claude_json_malformed():
    text = "not json at all"
    parsed = _parse_claude_json(text)
    assert parsed["decision"] == "ESCALATE"


def test_parse_claude_json_embedded():
    """Claude sometimes wraps JSON in prose. Should extract it."""
    text = "Here is my analysis:\n\n{\"decision\": \"CLEARED\", \"reasoning\": \"found it\"}\n\nLet me know if you need more."
    parsed = _parse_claude_json(text)
    assert parsed["decision"] == "CLEARED"


# ---------------------------------------------------------------------------
# _call_perplexity (mocked)
# ---------------------------------------------------------------------------


def test_call_perplexity_success():
    fake_response = {
        "choices": [{
            "message": {
                "content": "Mian Abdul Manan is a Pakistani politician born 1957.",
                "annotations": [
                    {"type": "url_citation", "url_citation": {
                        "url": "https://en.wikipedia.org/wiki/Mian_Abdul_Manan",
                        "title": "Mian Abdul Manan - Wikipedia",
                    }},
                ],
            },
        }],
        "usage": {"prompt_tokens": 50, "completion_tokens": 30},
    }

    with patch("app.investigator.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = fake_response
        mock_post.return_value = mock_resp

        answer, citations, usage = _call_perplexity("test question", "fake-key")

        assert "Pakistani politician" in answer
        assert len(citations) == 1
        assert citations[0]["url"] == "https://en.wikipedia.org/wiki/Mian_Abdul_Manan"
        assert usage["prompt_tokens"] == 50


def test_call_perplexity_top_level_citations_fallback():
    """Some API responses put citations at the top level instead of annotations."""
    fake_response = {
        "choices": [{"message": {"content": "answer text"}}],
        "citations": ["https://example.com/1", "https://example.com/2"],
        "usage": {},
    }
    with patch("app.investigator.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = fake_response
        mock_post.return_value = mock_resp

        answer, citations, _ = _call_perplexity("q", "key")
        assert answer == "answer text"
        assert len(citations) == 2
        assert citations[0]["url"] == "https://example.com/1"


def test_call_perplexity_http_error():
    import httpx
    with patch("app.investigator.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock(status_code=500)
        )
        mock_post.return_value = mock_resp

        answer, citations, usage = _call_perplexity("q", "key")
        assert answer == ""
        assert citations == []
        assert "error" in usage


def test_call_perplexity_timeout():
    import httpx
    with patch("app.investigator.httpx.post") as mock_post:
        mock_post.side_effect = httpx.ReadTimeout("slow")
        answer, citations, usage = _call_perplexity("q", "key")
        assert answer == ""
        assert usage["error"] == "timeout"


# ---------------------------------------------------------------------------
# _reason_with_claude (mocked)
# ---------------------------------------------------------------------------


def test_reason_with_claude_success():
    customer = {"name": "Abdul Manan", "dob": "2003-08-31"}
    match = make_match("Mian Abdul Manan", source="wikidata_peps")

    fake_claude_response = MagicMock()
    fake_claude_response.content = [MagicMock(text=json.dumps({
        "decision": "CLEARED",
        "contradictions": ["Age: politician served 2013-2018, customer was 10yo"],
        "supporting_findings": [],
        "reasoning": "Politician served before customer was old enough.",
    }))]
    fake_claude_response.usage = MagicMock(input_tokens=200, output_tokens=50)

    with patch("app.investigator.anthropic.Anthropic") as mock_anth:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_claude_response
        mock_anth.return_value = mock_client

        parsed, audit = _reason_with_claude(
            customer, match,
            "Mian Abdul Manan served as MNA from 2013-2018",
            [{"url": "https://wikipedia.org/...", "title": "Wiki"}],
            api_key="fake-key",
        )

        assert parsed["decision"] == "CLEARED"
        assert "politician" in parsed["reasoning"].lower() or "customer" in parsed["reasoning"].lower()
        assert audit["claude_input_tokens"] == 200


def test_reason_with_claude_no_api_key_escalates():
    import os
    # Ensure env has no key
    with patch.dict(os.environ, {}, clear=True):
        parsed, audit = _reason_with_claude(
            {"name": "X"}, make_match("Y"), "answer", [], api_key=None
        )
        assert parsed["decision"] == "ESCALATE"
        assert "no_api_key" in audit.get("error", "")


# ---------------------------------------------------------------------------
# investigate (end-to-end mocked)
# ---------------------------------------------------------------------------


def test_investigate_no_openrouter_key():
    import os
    with patch.dict(os.environ, {}, clear=True):
        decision, audit = investigate(
            {"name": "Test"}, make_match("Person"),
            openrouter_key=None, anthropic_key="x",
        )
        assert decision.decision == "ESCALATE"
        assert decision.cleared_by == "investigation_unavailable"


def test_investigate_full_pipeline_cleared():
    """End-to-end: Perplexity returns facts that let Claude clear the match."""
    customer = {"name": "Abdul Manan", "dob": "2003-08-31", "nationality": "Pakistan"}
    match = make_match(
        "Mian Abdul Manan",
        source="wikidata_peps",
        nationality=["Pakistan"],
        designation="Member of National Assembly",
    )

    # Mock Perplexity
    fake_pplx = {
        "choices": [{"message": {
            "content": "Mian Abdul Manan served as MNA from 2013 to 2018, born ~1957.",
            "annotations": [{
                "type": "url_citation",
                "url_citation": {"url": "https://wikipedia.org/wiki/Mian_Abdul_Manan", "title": "Wiki"},
            }],
        }}],
        "usage": {"prompt_tokens": 80, "completion_tokens": 40},
    }

    # Mock Claude
    fake_claude = MagicMock()
    fake_claude.content = [MagicMock(text=json.dumps({
        "decision": "CLEARED",
        "contradictions": ["Tenure 2013-2018 when customer was 10-15yo — impossible"],
        "supporting_findings": ["Both named 'Abdul Manan'", "Both Pakistani"],
        "reasoning": "Politician served 2013-2018; customer was a child then.",
    }))]
    fake_claude.usage = MagicMock(input_tokens=300, output_tokens=80)

    with patch("app.investigator.httpx.post") as mock_post, \
         patch("app.investigator.anthropic.Anthropic") as mock_anth:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = fake_pplx
        mock_post.return_value = mock_resp

        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_claude
        mock_anth.return_value = mock_client

        decision, audit = investigate(
            customer, match,
            openrouter_key="fake-or", anthropic_key="fake-anth",
        )

        assert decision.decision == "CLEARED"
        assert decision.cleared_by == "investigation"
        assert "politician" in decision.reasoning.lower() or "2013" in decision.reasoning
        assert audit["match_name"] == "Mian Abdul Manan"
        assert audit["perplexity_answer"]
        assert len(audit["perplexity_citations"]) == 1
        assert "claude_call" in audit


def test_investigate_perplexity_failure_stays_escalated():
    """If Perplexity fails, the match stays ESCALATE."""
    customer = {"name": "X"}
    match = make_match("Y")

    with patch("app.investigator.httpx.post") as mock_post:
        import httpx
        mock_post.side_effect = httpx.ReadTimeout("boom")

        decision, audit = investigate(
            customer, match,
            openrouter_key="fake-or", anthropic_key="fake-anth",
        )
        assert decision.decision == "ESCALATE"
        assert decision.cleared_by == "investigation_failed"


# ---------------------------------------------------------------------------
# investigate_escalations (batch)
# ---------------------------------------------------------------------------


def test_investigate_escalations_preserves_match_numbers():
    """Ensure match numbers are preserved through the investigation batch."""
    customer = {"name": "X"}
    matches = [
        (3, make_match("Person A")),
        (7, make_match("Person B")),
    ]

    # All will fail because no keys — but we should get back decisions with right numbers
    import os
    with patch.dict(os.environ, {}, clear=True):
        decisions, audits = investigate_escalations(customer, matches)
        assert len(decisions) == 2
        assert decisions[0].match_number == 3
        assert decisions[1].match_number == 7
        assert audits[0]["match_number"] == 3
        assert audits[1]["match_number"] == 7
