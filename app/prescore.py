"""Deterministic pre-score filter. Runs BEFORE Claude to auto-clear or auto-flag
obvious cases, reducing LLM calls and latency.

Rules applied in order:
  1. Exact CNIC/passport match -> auto_flag (reason="id_match")
  2. Gender conflict (both non-null, different) -> auto_clear (reason="gender")
  3. DOB >10 year gap (both have parseable years) -> auto_clear (reason="dob")
  4. Everything else -> send_to_llm
"""

from typing import Optional

from .schema import DeduplicatedMatch


def _extract_years(dob_list: list[str]) -> list[int]:
    """Extract 4-digit years from DOB strings (ISO dates, year-only, etc.)."""
    years = []
    for d in dob_list:
        d = d.strip()
        if not d:
            continue
        # Try to grab the year portion: "1985-03-12" -> 1985, "1985" -> 1985
        candidate = d[:4]
        try:
            year = int(candidate)
            if 1900 <= year <= 2100:
                years.append(year)
        except ValueError:
            continue
    return years


def _dob_gap_exceeds(user_dobs: list[str], match_dobs: list[str], gap: int = 10) -> bool:
    """Check if the minimum year gap between any user DOB and match DOB exceeds threshold."""
    user_years = _extract_years(user_dobs)
    match_years = _extract_years(match_dobs)

    if not user_years or not match_years:
        return False  # Can't determine, don't auto-clear

    min_gap = min(
        abs(uy - my) for uy in user_years for my in match_years
    )
    return min_gap > gap


def _temporal_impossibility(user_dobs: list[str], listed_on: str | None, min_age_at_listing: int = 15) -> bool:
    """Check if the candidate was listed before the customer could plausibly have been the listed person.

    If the candidate was `listed_on` a sanctions list in year Y, and the customer was born in year B,
    then the customer must have been at least `min_age_at_listing` years old in year Y (i.e., Y - B >= min_age_at_listing)
    to plausibly be that person. If Y - B < min_age_at_listing, the customer is too young to be the same person.

    Returns True if temporal impossibility is proven (customer cannot be the listed person).
    """
    if not listed_on:
        return False

    user_years = _extract_years(user_dobs)
    if not user_years:
        return False

    # Extract year from listed_on (ISO date, year-only, etc.)
    listed_year_candidates = _extract_years([listed_on])
    if not listed_year_candidates:
        return False
    listed_year = listed_year_candidates[0]

    # Use the earliest possible user birth year (most conservative — hardest to clear)
    earliest_user_birth = min(user_years)

    age_at_listing = listed_year - earliest_user_birth
    return age_at_listing < min_age_at_listing


def _has_id_match(user_identifiers: list[dict], match_identifiers: list[dict]) -> bool:
    """Check for exact CNIC or passport match between user and match."""
    if not user_identifiers or not match_identifiers:
        return False

    user_ids = set()
    for ident in user_identifiers:
        id_type = ident.get("type", "").strip().lower()
        id_value = ident.get("value", "").strip().lower()
        if id_type and id_value:
            user_ids.add(f"{id_type}:{id_value}")

    for ident in match_identifiers:
        id_type = ident.get("type", "").strip().lower()
        id_value = ident.get("value", "").strip().lower()
        if id_type and id_value:
            if f"{id_type}:{id_value}" in user_ids:
                return True

    return False


def _gender_conflict(user_gender: Optional[str], match_gender: Optional[str]) -> bool:
    """Check if both genders are non-null and different."""
    if not user_gender or not match_gender:
        return False
    return user_gender.strip().lower() != match_gender.strip().lower()


def prescore(
    user: dict, matches: list[DeduplicatedMatch]
) -> tuple[list, list, list]:
    """Apply deterministic rules to categorize matches before LLM evaluation.

    Args:
        user: Dict with user screening input. Expected keys:
              name, dob (list[str] or str), gender (str or None),
              identifiers (list[dict]).
        matches: Deduplicated match candidates.

    Returns:
        Tuple of (auto_cleared, auto_flagged, send_to_llm).
        Each is a list of dicts: {"match": DeduplicatedMatch, "reason": str}.
    """
    auto_cleared = []
    auto_flagged = []
    send_to_llm = []

    # Normalize user inputs
    user_gender = user.get("gender")
    user_dobs = user.get("dob", [])
    if isinstance(user_dobs, str):
        user_dobs = [user_dobs] if user_dobs.strip() else []
    user_identifiers = user.get("identifiers", [])

    for match in matches:
        rep = match.representative
        match_identifiers = match.all_identifiers or rep.identifiers

        # Rule 1: Exact CNIC/passport match -> auto_flag
        if _has_id_match(user_identifiers, match_identifiers):
            auto_flagged.append({
                "match": match,
                "reason": "id_match",
                "flagged_by": "rule:id",
            })
            continue

        # Rule 2: Gender conflict -> auto_clear
        if _gender_conflict(user_gender, rep.gender):
            auto_cleared.append({
                "match": match,
                "reason": "gender",
                "cleared_by": "rule:gender",
            })
            continue

        # Rule 3: DOB gap > 10 years -> auto_clear
        if _dob_gap_exceeds(user_dobs, rep.dob):
            auto_cleared.append({
                "match": match,
                "reason": "dob",
                "cleared_by": "rule:dob",
            })
            continue

        # Rule 4: Temporal impossibility -> auto_clear
        # If the candidate was listed on a sanctions list before the customer
        # was old enough to plausibly be the listed person, clear.
        listed_on = rep.listed_on
        # Also check across all sources — take earliest listing date
        if match.all_sources:
            for s in match.all_sources:
                s_listed = s.get("listed_on")
                if s_listed and (not listed_on or s_listed < listed_on):
                    listed_on = s_listed

        if _temporal_impossibility(user_dobs, listed_on):
            auto_cleared.append({
                "match": match,
                "reason": f"temporal_impossibility (listed {listed_on}, customer born {user_dobs[0] if user_dobs else '?'})",
                "cleared_by": "rule:temporal",
            })
            continue

        # Rule 5: Everything else -> send to LLM
        send_to_llm.append({
            "match": match,
            "reason": "needs_review",
        })

    return auto_cleared, auto_flagged, send_to_llm
