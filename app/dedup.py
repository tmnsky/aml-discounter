"""Cross-list deduplication: same person on OFAC+UN+EU+UK = 1 DeduplicatedMatch.

Groups candidates by shared identifiers, shared DOB + high name similarity,
or very high name similarity + shared nationality.
"""

import json
from typing import Optional

from rapidfuzz import fuzz

from .schema import DeduplicatedMatch, ListEntry


def _name_similarity(name_a: str, name_b: str) -> float:
    """Normalized name similarity using token_sort_ratio (0.0 - 1.0)."""
    return fuzz.token_sort_ratio(name_a.lower(), name_b.lower()) / 100.0


def _extract_id_values(identifiers: list[dict]) -> set[str]:
    """Extract normalized identifier values (type:value) for comparison."""
    values = set()
    for ident in identifiers:
        id_type = ident.get("type", "").strip().lower()
        id_value = ident.get("value", "").strip().lower()
        if id_type and id_value:
            values.add(f"{id_type}:{id_value}")
    return values


def _shared_identifiers(ids_a: set[str], ids_b: set[str]) -> bool:
    """Check if two identifier sets share any common identifier."""
    return bool(ids_a & ids_b)


def _shared_dob(dobs_a: list[str], dobs_b: list[str]) -> bool:
    """Check if two DOB lists share any common date."""
    if not dobs_a or not dobs_b:
        return False
    set_a = {d.strip() for d in dobs_a if d.strip()}
    set_b = {d.strip() for d in dobs_b if d.strip()}
    return bool(set_a & set_b)


def _shared_nationality(nats_a: list[str], nats_b: list[str]) -> bool:
    """Check if two nationality lists share any common country code."""
    if not nats_a or not nats_b:
        return False
    set_a = {n.strip().upper() for n in nats_a if n.strip()}
    set_b = {n.strip().upper() for n in nats_b if n.strip()}
    return bool(set_a & set_b)


def _count_non_empty(candidate: dict) -> int:
    """Count non-empty fields to determine the 'richest' record."""
    count = 0
    for key, val in candidate.items():
        if key in ("score", "rank"):
            continue
        if val is None:
            continue
        if isinstance(val, str) and val.strip():
            count += 1
        elif isinstance(val, list) and val:
            count += 1
        elif isinstance(val, dict) and val:
            count += 1
        elif isinstance(val, (int, float, bool)):
            count += 1
    return count


def _candidate_to_list_entry(c: dict) -> ListEntry:
    """Convert a candidate dict back to a ListEntry for the representative field."""
    return ListEntry(
        id=c.get("id", ""),
        source=c.get("source", ""),
        list_name=c.get("list_name", ""),
        names=c.get("names", [c.get("name", "")]),
        dob=c.get("dob", []),
        dob_approximate=c.get("dob_approximate", False),
        pob=c.get("pob", []),
        nationality=c.get("nationality", []),
        gender=c.get("gender"),
        identifiers=c.get("identifiers", []),
        addresses=c.get("addresses", []),
        designation=c.get("designation"),
        listing_reason=c.get("listing_reason"),
        listed_on=c.get("listed_on"),
        programs=c.get("programs", []),
        source_url=c.get("source_url", ""),
    )


def _should_merge(a: dict, b: dict) -> tuple[bool, bool]:
    """Determine if two candidates represent the same person.

    Returns (should_merge, uncertain). uncertain=True means the merge
    happened without a shared DOB or shared identifier.
    """
    ids_a = _extract_id_values(a.get("identifiers", []))
    ids_b = _extract_id_values(b.get("identifiers", []))

    # Rule 1: Shared identifier (CNIC, passport, etc.) -> definite match
    if _shared_identifiers(ids_a, ids_b):
        return True, False

    name_a = a.get("name", "")
    name_b = b.get("name", "")
    sim = _name_similarity(name_a, name_b)

    # Rule 2: name_sim > 0.90 AND shared DOB -> same person
    if sim > 0.90 and _shared_dob(a.get("dob", []), b.get("dob", [])):
        return True, False

    # Rule 3: name_sim > 0.95 AND shared nationality -> same person
    if sim > 0.95 and _shared_nationality(
        a.get("nationality", []), b.get("nationality", [])
    ):
        # No shared DOB and no shared ID -> uncertain
        has_shared_dob = _shared_dob(a.get("dob", []), b.get("dob", []))
        has_shared_id = _shared_identifiers(ids_a, ids_b)
        uncertain = not has_shared_dob and not has_shared_id
        return True, uncertain

    # Rule 4: Near-identical names from DIFFERENT sources -> merge
    # When the same person appears on multiple sanctions lists (e.g., UN + OFAC + EU + UK),
    # they often have near-identical names but the lists don't publish DOB/nationality.
    # Avoid creating N separate matches for the same person.
    src_a = a.get("source", "")
    src_b = b.get("source", "")
    if sim > 0.97 and src_a != src_b and src_a and src_b:
        # Name is near-identical and from different source lists — very likely same person
        return True, True  # Mark as uncertain so AI is warned

    return False, False


def dedup_candidates(candidates: list[dict]) -> list[DeduplicatedMatch]:
    """Deduplicate candidates across sanctions lists into unique persons.

    Uses union-find to group candidates that should be merged, then builds
    a DeduplicatedMatch per group with the richest record as representative.
    """
    if not candidates:
        return []

    n = len(candidates)

    # Union-Find
    parent = list(range(n))
    uncertain_edges: set[tuple[int, int]] = set()

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    # Compare all pairs (N is bounded by max_results, typically <= 200)
    for i in range(n):
        for j in range(i + 1, n):
            merge, uncertain = _should_merge(candidates[i], candidates[j])
            if merge:
                union(i, j)
                if uncertain:
                    uncertain_edges.add((min(i, j), max(i, j)))

    # Group by root
    groups: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        groups.setdefault(root, []).append(i)

    # Check if a group has any uncertain edge
    def group_has_uncertain(indices: list[int]) -> bool:
        idx_set = set(indices)
        for a, b in uncertain_edges:
            if a in idx_set or b in idx_set:
                return True
        return False

    # Build DeduplicatedMatch per group
    results = []
    for root, indices in groups.items():
        members = [candidates[i] for i in indices]

        # Pick richest record as representative
        richest = max(members, key=_count_non_empty)
        representative = _candidate_to_list_entry(richest)

        # Collect all sources
        all_sources = []
        seen_sources = set()
        for m in members:
            key = (m.get("source", ""), m.get("id", ""))
            if key not in seen_sources:
                seen_sources.add(key)
                all_sources.append({
                    "source": m.get("source", ""),
                    "list_name": m.get("list_name", ""),
                    "source_url": m.get("source_url", ""),
                    "listed_on": m.get("listed_on"),
                    "programs": m.get("programs", []),
                })

        # Collect all unique names
        all_names = []
        seen_names = set()
        for m in members:
            for name in m.get("names", [m.get("name", "")]):
                if name and name.lower() not in seen_names:
                    seen_names.add(name.lower())
                    all_names.append(name)

        # Collect all unique identifiers
        all_identifiers = []
        seen_ids = set()
        for m in members:
            for ident in m.get("identifiers", []):
                id_key = (
                    ident.get("type", "").lower(),
                    ident.get("value", "").lower(),
                )
                if id_key not in seen_ids:
                    seen_ids.add(id_key)
                    all_identifiers.append(ident)

        uncertain = group_has_uncertain(indices)

        results.append(DeduplicatedMatch(
            representative=representative,
            all_sources=all_sources,
            all_names=all_names,
            all_identifiers=all_identifiers,
            uncertain_merge=uncertain,
        ))

    # Sort by best score in group (descending)
    def _best_score(dm: DeduplicatedMatch) -> float:
        # Find the best score among original candidates in this group
        for c in candidates:
            if c.get("id") == dm.representative.id:
                return c.get("score", 0)
        return 0

    results.sort(key=_best_score, reverse=True)
    return results
