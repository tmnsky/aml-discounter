"""Claude-powered false positive discounting via batched inference."""

import json
import logging
import os
import random
import time
from typing import Optional

import anthropic

from .schema import DeduplicatedMatch, MatchDecision

logger = logging.getLogger(__name__)

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_BATCH = 25  # split into multiple calls if >25 matches

SYSTEM_PROMPT = """You are a sanctions and PEP screening specialist. Your task is to evaluate whether a customer record matches any of the listed sanctions/PEP candidates.

## Core Principle
Look for CONTRADICTIONS, not similarities. Name variations, transliterations, aliases, and missing fields are normal. Missing data is NOT evidence against a match. Only explicit contradictions (different birth dates, conflicting ID numbers, incompatible nationalities, or temporal impossibilities) justify clearing a candidate.

CRITICAL: Base your analysis ONLY on the field values provided in the records below. Do NOT use knowledge from training data about these entities. If a field is absent, mark it as unknown, not as evidence for or against a match.

## Contradictions That Justify CLEARED

Look for all of these:

1. **DOB conflict**: Customer DOB and candidate DOB differ by >5 years (when both are stated).
2. **Gender conflict**: Different genders when both are stated.
3. **ID conflict**: Conflicting national IDs, passport numbers, or CNICs (that cannot both be correct for the same person).
4. **Nationality conflict**: Incompatible nationalities with no plausible dual-citizenship explanation.
5. **Temporal impossibility**: The candidate was **listed** (added to a sanctions list) BEFORE the customer's DOB, or within a few years of it. People are typically sanctioned as adults for crimes/associations; if the listing predates the customer's birth, they cannot be the same person. Example: candidate listed 2001-10-17 for Al-Qaida involvement, customer born 2003 → clearly different person.
6. **Age-at-listing impossibility**: The candidate's role/designation at time of listing implies they were at least ~18 years old then. If `listed_on` minus `customer.dob_year` < 15 years, the customer was too young to have been that person.
7. **Role/profile mismatch**: Customer is plainly a civilian applicant (22-year-old applying for a wallet); candidate is clearly a military commander, cleric, or senior official from the listing narrative.
8. **Father's name mismatch**: In patronymic cultures (South Asia, Middle East, Afghanistan), father's name is a key identifier. If both the customer and candidate have stated father's names and they differ significantly (not just transliteration variants), this is strong evidence of different people.

## Weighing Alias Matches
If the match is on a WEAK alias rather than the primary name, treat the match as weaker evidence. If the match is on a GOOD (strong) alias, treat it as near-primary-name strength.

## Verdict Definitions
- CLEARED: You found explicit contradictory evidence that proves this is a different person (any of the 7 contradiction types above).
- LIKELY_MATCH: No contradictions found. Names are consistent. Biographical details align or are absent on both sides.
- ESCALATE: Genuinely ambiguous. Some supporting similarity, no contradictions, but not enough confidence. Human review needed.

Default to CLEARED when temporal impossibility is clear, even if other biographical fields are missing.

## Output Format
Return a JSON array with one object per candidate evaluated."""


def _format_user(user: dict) -> str:
    """Format user record for the prompt."""
    lines = ["CUSTOMER RECORD:"]
    field_map = {
        "name": "Name",
        "dob": "Date of Birth",
        "nationality": "Nationality",
        "gender": "Gender",
        "cnic": "CNIC/National ID",
        "passport": "Passport",
        "pob": "Place of Birth",
        "father_name": "Father's Name",
        "address": "Address",
        "notes": "Additional Notes",
    }
    for key, label in field_map.items():
        val = user.get(key)
        if val:
            lines.append(f"  {label}: {val}")

    # Compute approximate customer age / birth year for temporal reasoning
    dob = user.get("dob", "")
    if dob:
        try:
            year = int(dob[:4])
            from datetime import datetime
            current_year = datetime.utcnow().year
            age = current_year - year
            lines.append(f"  Approximate Age: {age}  (born ~{year})")
        except (ValueError, IndexError):
            pass

    return "\n".join(lines)


def _format_match(idx: int, match: DeduplicatedMatch) -> str:
    """Format a single deduped match for the prompt."""
    rep = match.representative
    sources = ", ".join(
        f"{s['list_name']}" for s in match.all_sources
    ) if match.all_sources else rep.list_name

    lines = [f"[{idx}] Candidate (appears on: {sources}):"]
    lines.append(f"  Primary Name: {rep.names[0] if rep.names else 'Unknown'}")
    if len(match.all_names) > 1:
        aliases = [n for n in match.all_names[1:] if n != rep.names[0]][:5]
        if aliases:
            # Show alias quality if available
            quality_note = ""
            if rep.alias_quality:
                quality_strong = [q for q in rep.alias_quality if q in ("strong", "Good")]
                quality_weak = [q for q in rep.alias_quality if q in ("weak", "Low")]
                if quality_strong and quality_weak:
                    quality_note = " (mix of strong and weak aliases)"
                elif quality_weak:
                    quality_note = " (WEAK aliases — lower match confidence)"
                elif quality_strong:
                    quality_note = " (strong aliases)"
            lines.append(f"  Aliases: {', '.join(aliases)}{quality_note}")
    # Surface listed_on prominently — it's critical for temporal reasoning
    if rep.listed_on:
        lines.append(f"  LISTED ON: {rep.listed_on}  ← sanctioned/listed on this date")
    if rep.programs:
        lines.append(f"  Programs: {', '.join(rep.programs[:5])}")
    if rep.designation:
        lines.append(f"  Designation: {rep.designation}")
    if rep.dob:
        dob_str = ", ".join(rep.dob)
        if rep.dob_approximate:
            dob_str = f"circa {dob_str}"
        lines.append(f"  DOB: {dob_str}")
    else:
        lines.append(f"  DOB: (not listed)")
    if rep.pob:
        lines.append(f"  Place of Birth: {', '.join(rep.pob)}")
    if rep.nationality:
        lines.append(f"  Nationality: {', '.join(rep.nationality)}")
    if rep.gender:
        lines.append(f"  Gender: {rep.gender}")
    if rep.father_name:
        lines.append(f"  Father's Name: {rep.father_name}")
    if match.all_identifiers and any(i.get("value") for i in match.all_identifiers):
        id_strs = [f"{d.get('type', 'id')}: {d['value']}" for d in match.all_identifiers[:5] if d.get("value")]
        if id_strs:
            lines.append(f"  Identifiers: {'; '.join(id_strs)}")
    if rep.listing_reason:
        # Sanitize to prevent prompt injection
        reason = rep.listing_reason[:300].replace("\n", " ")
        lines.append(f"  Listing Narrative: {reason}")
    if match.uncertain_merge:
        lines.append("  NOTE: This entry may combine records from different lists that could be distinct individuals (no shared DOB or ID to confirm).")
    return "\n".join(lines)


def _build_prompt(user: dict, matches: list[DeduplicatedMatch]) -> str:
    """Build the user message for Claude."""
    user_block = _format_user(user)

    # Randomize match order to mitigate position bias
    indexed_matches = list(enumerate(matches, 1))
    random.shuffle(indexed_matches)

    match_blocks = []
    for orig_idx, match in indexed_matches:
        match_blocks.append(_format_match(orig_idx, match))

    matches_block = "\n\n".join(match_blocks)

    return f"""{user_block}

CANDIDATES TO EVALUATE ({len(matches)} unique persons):

{matches_block}

For each candidate, return a JSON array. Each element must have:
- "match_number": the candidate number shown in brackets above
- "verdict": "CLEARED" or "LIKELY_MATCH" or "ESCALATE"
- "contradictions": array of strings like "DOB: customer=1994 vs candidate=~1975 (19yr gap)"
- "supporting_similarities": array of strings like "Nationality: both Pakistan"
- "reasoning": one sentence explaining the verdict"""


def _parse_response(content: str, expected_count: int) -> list[dict]:
    """Parse and validate Claude's JSON response."""
    # Try to extract JSON array from the response
    text = content.strip()
    if text.startswith("```"):
        # Strip markdown code fences
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        text = text.strip()

    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array, got {type(data).__name__}")

    decisions = []
    for item in data:
        decisions.append({
            "match_number": int(item.get("match_number", 0)),
            "verdict": str(item.get("verdict", "ESCALATE")).upper(),
            "contradictions": item.get("contradictions", []),
            "supporting_similarities": item.get("supporting_similarities", []),
            "reasoning": str(item.get("reasoning", "")),
        })

    # Validate verdicts
    valid_verdicts = {"CLEARED", "LIKELY_MATCH", "ESCALATE"}
    for d in decisions:
        if d["verdict"] not in valid_verdicts:
            d["verdict"] = "ESCALATE"

    return decisions


def discount_matches(
    user: dict,
    matches: list[DeduplicatedMatch],
    api_key: Optional[str] = None,
) -> tuple[list[MatchDecision], list[dict]]:
    """
    Send ambiguous matches to Claude for evaluation.

    Returns:
        (decisions, llm_calls) - decisions per match, and raw LLM I/O for audit
    """
    if not matches:
        return [], []

    key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        logger.error("ANTHROPIC_API_KEY not set, marking all as ESCALATE")
        return _escalate_all(matches, "No API key configured"), []

    client = anthropic.Anthropic(api_key=key)
    all_decisions: list[MatchDecision] = []
    all_llm_calls: list[dict] = []

    # Split into batches if needed
    batches = []
    for i in range(0, len(matches), MAX_BATCH):
        batches.append(matches[i:i + MAX_BATCH])

    for batch_idx, batch in enumerate(batches):
        prompt = _build_prompt(user, batch)
        llm_call = {
            "batch": batch_idx + 1,
            "match_count": len(batch),
            "model": MODEL,
            "prompt_preview": prompt[:500] + "..." if len(prompt) > 500 else prompt,
            "full_prompt": prompt,
        }

        try:
            t0 = time.time()
            response = client.messages.create(
                model=MODEL,
                max_tokens=8000,
                temperature=0,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": prompt}],
            )

            elapsed_ms = int((time.time() - t0) * 1000)
            response_text = response.content[0].text if response.content else ""

            llm_call["response_preview"] = response_text[:500] + "..." if len(response_text) > 500 else response_text
            llm_call["full_response"] = response_text
            llm_call["elapsed_ms"] = elapsed_ms
            llm_call["input_tokens"] = response.usage.input_tokens
            llm_call["output_tokens"] = response.usage.output_tokens
            llm_call["cache_read_tokens"] = getattr(response.usage, "cache_read_input_tokens", 0)

            try:
                parsed = _parse_response(response_text, len(batch))
                llm_call["status"] = "ok"

                # Map parsed decisions back to matches
                decision_map = {d["match_number"]: d for d in parsed}
                for i, match in enumerate(batch, 1):
                    d = decision_map.get(i, {})
                    all_decisions.append(MatchDecision(
                        match_number=i + (batch_idx * MAX_BATCH),
                        decision=d.get("verdict", "ESCALATE"),
                        contradictions=[{"detail": c} if isinstance(c, str) else c for c in d.get("contradictions", [])],
                        supporting_similarities=[{"detail": s} if isinstance(s, str) else s for s in d.get("supporting_similarities", [])],
                        reasoning=d.get("reasoning", "No reasoning provided"),
                        cleared_by="ai",
                    ))

            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Failed to parse Claude response (batch {batch_idx+1}): {e}. Retrying once.")
                llm_call["parse_error"] = str(e)

                # Retry once with explicit correction
                try:
                    retry_response = client.messages.create(
                        model=MODEL,
                        max_tokens=8000,
                        temperature=0,
                        system=[{
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }],
                        messages=[
                            {"role": "user", "content": prompt},
                            {"role": "assistant", "content": response_text},
                            {"role": "user", "content": "Your response was not valid JSON. Please return ONLY a JSON array with the exact schema specified. No markdown, no explanation, just the JSON array."},
                        ],
                    )
                    retry_text = retry_response.content[0].text if retry_response.content else ""
                    parsed = _parse_response(retry_text, len(batch))
                    llm_call["retry_status"] = "ok"
                    llm_call["retry_response"] = retry_text

                    decision_map = {d["match_number"]: d for d in parsed}
                    for i, match in enumerate(batch, 1):
                        d = decision_map.get(i, {})
                        all_decisions.append(MatchDecision(
                            match_number=i + (batch_idx * MAX_BATCH),
                            decision=d.get("verdict", "ESCALATE"),
                            contradictions=[{"detail": c} if isinstance(c, str) else c for c in d.get("contradictions", [])],
                            supporting_similarities=[{"detail": s} if isinstance(s, str) else s for s in d.get("supporting_similarities", [])],
                            reasoning=d.get("reasoning", "Parse retry succeeded"),
                            cleared_by="ai",
                        ))
                except Exception as retry_err:
                    logger.error(f"Retry also failed: {retry_err}. Escalating all in batch.")
                    llm_call["retry_status"] = "failed"
                    llm_call["retry_error"] = str(retry_err)
                    all_decisions.extend(_escalate_all(batch, f"JSON parse failed after retry: {e}", batch_idx * MAX_BATCH))

        except anthropic.RateLimitError as e:
            logger.warning(f"Rate limited by Claude API: {e}. Backing off and escalating batch.")
            llm_call["status"] = "rate_limited"
            llm_call["error"] = str(e)
            all_decisions.extend(_escalate_all(batch, f"Rate limited: {e}", batch_idx * MAX_BATCH))
            time.sleep(5)  # Basic backoff

        except anthropic.APIError as e:
            logger.error(f"Claude API error: {e}. Escalating all in batch.")
            llm_call["status"] = "api_error"
            llm_call["error"] = str(e)
            all_decisions.extend(_escalate_all(batch, f"API error: {e}", batch_idx * MAX_BATCH))

        except Exception as e:
            logger.error(f"Unexpected error calling Claude: {e}. Escalating all in batch.")
            llm_call["status"] = "error"
            llm_call["error"] = str(e)
            all_decisions.extend(_escalate_all(batch, f"Unexpected error: {e}", batch_idx * MAX_BATCH))

        all_llm_calls.append(llm_call)

    return all_decisions, all_llm_calls


def _escalate_all(matches: list[DeduplicatedMatch], reason: str, offset: int = 0) -> list[MatchDecision]:
    """Mark all matches as ESCALATE (fallback on any error)."""
    return [
        MatchDecision(
            match_number=i + 1 + offset,
            decision="ESCALATE",
            reasoning=f"Automatically escalated: {reason}",
            cleared_by="error_fallback",
        )
        for i, _ in enumerate(matches)
    ]
