"""Pass 2 investigation: resolve ESCALATE matches via Perplexity web research + Claude reasoning.

Pipeline:
  1. Build a focused factual question about the matched person
  2. Call Perplexity sonar-pro-search via OpenRouter (returns answer + citations)
  3. Give Claude Sonnet the customer + match + Perplexity findings
  4. Return a final verdict (CLEARED / LIKELY_MATCH / ESCALATE) + audit record
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import anthropic
import httpx

from .schema import DeduplicatedMatch, MatchDecision

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
PERPLEXITY_MODEL = "perplexity/sonar-pro-search"
CLAUDE_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
PERPLEXITY_TIMEOUT = 90.0
CLAUDE_TIMEOUT = 120.0


INVESTIGATION_SYSTEM_PROMPT = """You are an AML compliance specialist resolving an escalated sanctions/PEP match.

Pass 1 couldn't confidently clear or flag this match. You now have web research findings from Perplexity to work with.

## Your task
Decide whether the customer is or is not the sanctioned/PEP person. Use the research findings as evidence.

## Rules
- Default to CLEARED if you find explicit contradictions with the web research (age, tenure, death, nationality, role).
- Default to LIKELY_MATCH if the research confirms details align with the customer.
- Stay ESCALATE only if the research is inconclusive AND you cannot make a confident determination.

## Contradictions to check
1. Age: if research states the person's DOB or age, compare against customer's DOB. >5 year gap = CLEARED.
2. Tenure dates: if research shows the person held their role in specific years, check feasibility. A PEP who served 2013-2018 cannot be the same as a customer born in 2003 (they'd have been 10 years old when the tenure started).
3. Death: if research confirms the person is deceased, check death date vs customer's current applicability.
4. Nationality: if research shows different nationality with no dual-citizenship plausibility.
5. Role/profile mismatch: research describes a senior military officer / cleric / official incompatible with the customer's profile.
6. Temporal impossibility: listing/activity dates predate customer's plausible adult age.

## Output
Return JSON only:
{
  "decision": "CLEARED" | "LIKELY_MATCH" | "ESCALATE",
  "contradictions": ["field: research says X, customer says Y"],
  "supporting_findings": ["finding from research that informed the decision"],
  "reasoning": "One or two sentences explaining the verdict, citing the research."
}"""


def _build_question(customer: dict, match: DeduplicatedMatch) -> str:
    """Build a targeted factual question for Perplexity based on the match type."""
    rep = match.representative
    match_name = rep.names[0] if rep.names else "Unknown"
    sources = ", ".join(s.get("list_name", "") for s in match.all_sources) if match.all_sources else rep.list_name

    # Classify: is this a sanctions hit or a PEP hit?
    source_codes = {s.get("source", "") for s in match.all_sources} | {rep.source}
    is_pep = any(s in source_codes for s in (
        "wikidata_peps", "us_congress", "uk_parliament", "eu_parliament"
    ))

    if is_pep:
        # PEP case: look up the politician's bio
        return (
            f"Who is {match_name}"
            + (f" from {rep.nationality[0]}" if rep.nationality else "")
            + f"? I need their date of birth, age, political role, and years served. "
            + f"They appear on a PEP list ({sources}). "
            + (f"Their listed role is: {rep.designation}. " if rep.designation else "")
            + "If there are multiple people with this name, describe each briefly."
        )
    else:
        # Sanctions case: look up the listing history
        listed_on = rep.listed_on or "unknown date"
        programs = ", ".join(rep.programs[:3]) if rep.programs else "sanctions"
        return (
            f"Who is {match_name}, a person listed on {sources} sanctions lists"
            + (f" (listed {listed_on})" if rep.listed_on else "")
            + f"? I need their date of birth, nationality, role, and the original reason "
            + f"they were sanctioned under {programs}. "
            + "When did their activities begin? If they are deceased, when did they die?"
        )


def _call_perplexity(question: str, api_key: str) -> tuple[str, list[dict], dict]:
    """Call Perplexity via OpenRouter. Returns (answer_text, citations, usage_info)."""
    try:
        resp = httpx.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/zarpay/aml-discounter",
                "X-Title": "AML Discounter",
            },
            json={
                "model": PERPLEXITY_MODEL,
                "messages": [{"role": "user", "content": question}],
            },
            timeout=PERPLEXITY_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {}) if choice else {}
        answer = message.get("content", "") or ""

        # Extract citations — OpenRouter returns them as annotations with url_citation
        citations: list[dict] = []
        for ann in message.get("annotations", []) or []:
            if ann.get("type") == "url_citation":
                uc = ann.get("url_citation", {})
                citations.append({
                    "url": uc.get("url", ""),
                    "title": uc.get("title", ""),
                })

        # Some responses put citations at the top level
        if not citations:
            for url in data.get("citations", []) or []:
                citations.append({"url": url, "title": ""})

        usage = data.get("usage", {}) or {}
        return answer, citations, usage

    except httpx.HTTPStatusError as e:
        logger.warning("Perplexity HTTP error: %s", e)
        return "", [], {"error": str(e)}
    except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
        logger.warning("Perplexity timeout: %s", e)
        return "", [], {"error": "timeout"}
    except Exception as e:
        logger.warning("Perplexity unexpected error: %s", e)
        return "", [], {"error": str(e)}


def _format_match_for_investigation(match: DeduplicatedMatch) -> str:
    """Format the match record for the Claude reasoning call."""
    rep = match.representative
    sources = ", ".join(s.get("list_name", "") for s in match.all_sources) if match.all_sources else rep.list_name
    lines = [f"MATCH RECORD (from {sources}):"]
    lines.append(f"  Name: {rep.names[0] if rep.names else 'Unknown'}")
    if len(match.all_names) > 1:
        lines.append(f"  Aliases: {', '.join(match.all_names[1:5])}")
    if rep.listed_on:
        lines.append(f"  Listed On: {rep.listed_on}")
    if rep.programs:
        lines.append(f"  Programs: {', '.join(rep.programs[:5])}")
    if rep.designation:
        lines.append(f"  Designation/Role: {rep.designation}")
    if rep.dob:
        lines.append(f"  DOB: {', '.join(rep.dob)}")
    if rep.nationality:
        lines.append(f"  Nationality: {', '.join(rep.nationality)}")
    if rep.listing_reason:
        lines.append(f"  Listing Narrative: {rep.listing_reason[:300]}")
    return "\n".join(lines)


def _format_customer(customer: dict) -> str:
    """Format customer record for the Claude reasoning call."""
    lines = ["CUSTOMER RECORD:"]
    for key, label in [
        ("name", "Name"), ("dob", "DOB"), ("nationality", "Nationality"),
        ("gender", "Gender"), ("cnic", "CNIC/ID"), ("passport", "Passport"),
        ("pob", "Place of Birth"), ("notes", "Notes"),
    ]:
        val = customer.get(key)
        if val:
            lines.append(f"  {label}: {val}")

    # Compute age for reasoning
    dob = customer.get("dob", "")
    if dob and len(dob) >= 4:
        try:
            from datetime import datetime, timezone
            year = int(dob[:4])
            current_year = datetime.now(timezone.utc).year
            lines.append(f"  Approximate Age: {current_year - year} (born ~{year})")
        except (ValueError, IndexError):
            pass
    return "\n".join(lines)


def _reason_with_claude(
    customer: dict, match: DeduplicatedMatch, perplexity_answer: str,
    citations: list[dict], api_key: Optional[str]
) -> tuple[dict, dict]:
    """Use Claude Sonnet to reason over Perplexity's findings.

    Returns (parsed_decision_dict, raw_claude_call_info).
    """
    if not api_key:
        api_key = os.getenv("ANTHROPIC_API_KEY")

    if not api_key:
        return (
            {"decision": "ESCALATE", "reasoning": "No Anthropic API key configured for investigation"},
            {"error": "no_api_key"},
        )

    client = anthropic.Anthropic(api_key=api_key, timeout=CLAUDE_TIMEOUT)

    prompt = f"""{_format_customer(customer)}

{_format_match_for_investigation(match)}

WEB RESEARCH FINDINGS (from Perplexity sonar-pro-search):
{perplexity_answer if perplexity_answer else '(No research findings available — research call failed)'}

CITATIONS:
{chr(10).join(f'  - {c.get("title", "") or c.get("url", "")}: {c.get("url", "")}' for c in citations[:10]) if citations else '(no citations)'}

Based on the research findings, decide whether this customer is the same person as the match.
Return JSON only."""

    raw_call = {
        "question_sent_to_perplexity": "",  # filled in by caller
        "perplexity_answer_preview": perplexity_answer[:500] if perplexity_answer else "",
        "citation_count": len(citations),
        "claude_model": CLAUDE_MODEL,
    }

    try:
        t0 = time.time()
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            temperature=0,
            system=[{
                "type": "text",
                "text": INVESTIGATION_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
        )
        elapsed = int((time.time() - t0) * 1000)
        content = response.content[0].text if response.content else "{}"

        raw_call["claude_elapsed_ms"] = elapsed
        raw_call["claude_input_tokens"] = response.usage.input_tokens
        raw_call["claude_output_tokens"] = response.usage.output_tokens
        raw_call["claude_response"] = content

        parsed = _parse_claude_json(content)
        return parsed, raw_call

    except anthropic.APIError as e:
        logger.error("Claude investigation error: %s", e)
        raw_call["error"] = f"claude_api_error: {e}"
        return (
            {"decision": "ESCALATE", "reasoning": f"Claude API error during investigation: {e}"},
            raw_call,
        )
    except Exception as e:
        logger.error("Unexpected Claude error: %s", e)
        raw_call["error"] = f"unexpected: {e}"
        return (
            {"decision": "ESCALATE", "reasoning": f"Unexpected error during investigation: {e}"},
            raw_call,
        )


def _parse_claude_json(text: str) -> dict:
    """Parse Claude's JSON response, tolerating markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find a JSON object in the text
        import re
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return {"decision": "ESCALATE", "reasoning": "Claude returned malformed JSON"}
        else:
            return {"decision": "ESCALATE", "reasoning": "Claude returned no parseable JSON"}

    # Normalize
    decision = str(data.get("decision", "ESCALATE")).upper()
    if decision not in ("CLEARED", "LIKELY_MATCH", "ESCALATE"):
        decision = "ESCALATE"

    return {
        "decision": decision,
        "contradictions": data.get("contradictions", []),
        "supporting_findings": data.get("supporting_findings", []),
        "reasoning": data.get("reasoning", ""),
    }


def investigate(
    customer: dict,
    match: DeduplicatedMatch,
    openrouter_key: Optional[str] = None,
    anthropic_key: Optional[str] = None,
) -> tuple[MatchDecision, dict]:
    """Investigate a single escalated match. Returns (new_decision, audit_record)."""
    openrouter_key = openrouter_key or os.getenv("OPENROUTER_API_KEY")

    audit: dict = {
        "match_name": match.representative.names[0] if match.representative.names else "",
        "match_sources": [s.get("list_name", "") for s in match.all_sources],
    }

    if not openrouter_key:
        logger.warning("OPENROUTER_API_KEY not set, cannot investigate")
        audit["error"] = "no_openrouter_key"
        return (
            MatchDecision(
                match_number=0,
                decision="ESCALATE",
                reasoning="Investigation unavailable: OPENROUTER_API_KEY not configured",
                cleared_by="investigation_unavailable",
            ),
            audit,
        )

    # 1. Build question
    question = _build_question(customer, match)
    audit["question"] = question

    # 2. Call Perplexity
    answer, citations, pp_usage = _call_perplexity(question, openrouter_key)
    audit["perplexity_answer"] = answer
    audit["perplexity_citations"] = citations
    audit["perplexity_usage"] = pp_usage

    if not answer:
        # Perplexity failed — stay ESCALATE
        return (
            MatchDecision(
                match_number=0,
                decision="ESCALATE",
                reasoning=f"Investigation could not retrieve web research: {pp_usage.get('error', 'no answer')}",
                cleared_by="investigation_failed",
            ),
            audit,
        )

    # 3. Claude reasoning over the findings
    parsed, claude_audit = _reason_with_claude(customer, match, answer, citations, anthropic_key)
    claude_audit["question_sent_to_perplexity"] = question
    audit["claude_call"] = claude_audit

    # 4. Build MatchDecision
    return (
        MatchDecision(
            match_number=0,  # caller assigns
            decision=parsed["decision"],
            contradictions=[
                {"detail": c} if isinstance(c, str) else c
                for c in parsed.get("contradictions", [])
            ],
            supporting_similarities=[
                {"detail": s} if isinstance(s, str) else s
                for s in parsed.get("supporting_findings", [])
            ],
            reasoning=parsed.get("reasoning", ""),
            cleared_by="investigation",
        ),
        audit,
    )


def investigate_escalations(
    customer: dict,
    escalations: list[tuple[int, DeduplicatedMatch]],
    openrouter_key: Optional[str] = None,
    anthropic_key: Optional[str] = None,
) -> tuple[list[MatchDecision], list[dict]]:
    """Run investigations sequentially on all escalated matches.

    Args:
        customer: customer dict
        escalations: list of (match_number, DeduplicatedMatch) to investigate

    Returns:
        (new_decisions, audit_records) — same length as escalations
    """
    new_decisions: list[MatchDecision] = []
    audit_records: list[dict] = []

    for match_number, match in escalations:
        logger.info("Investigating escalation %d: %s", match_number, match.representative.names[0] if match.representative.names else "?")
        decision, audit = investigate(customer, match, openrouter_key, anthropic_key)
        decision.match_number = match_number
        audit["match_number"] = match_number
        new_decisions.append(decision)
        audit_records.append(audit)

    return new_decisions, audit_records
