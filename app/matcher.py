"""Multi-pass name matching against SQLite FTS5 sanctions index.

Stage 1: FTS5 full-text search with transliterated + phonetic variants (~10ms)
Stage 2: rapidfuzz re-ranking with token_sort_ratio, token_set_ratio, WRatio (~50ms)
"""

import re
import sqlite3
from typing import Optional

from jellyfish import metaphone
from rapidfuzz import fuzz

try:
    from icu import Transliterator
    _translit = Transliterator.createInstance("Any-Latin; Latin-ASCII; Lower")
    _HAS_ICU = True
except ImportError:
    _translit = None
    _HAS_ICU = False

# ---------------------------------------------------------------------------
# Transliteration & normalization
# ---------------------------------------------------------------------------

# Characters that are special in FTS5 query syntax
_FTS5_SPECIAL = re.compile(r'[+\-*"():\^]')
# Collapse whitespace
_MULTI_SPACE = re.compile(r"\s+")
# Honorifics / noise tokens that hurt matching
_NOISE_TOKENS = {
    "mr", "mrs", "ms", "dr", "prof", "haji", "hajj", "maulvi",
    "maulana", "sheikh", "shaykh", "syed", "sayyid", "bin", "ibn",
    "bint", "al", "el", "ul", "abu", "abd", "della", "von", "van",
    "de", "di", "du", "le", "la", "das", "dos", "del",
}


def transliterate(name: str) -> str:
    """Convert any script to Latin-ASCII lowercase via ICU (or fallback to basic ASCII folding)."""
    if _HAS_ICU and _translit:
        return _translit.transliterate(name)
    # Fallback: basic ASCII folding via unicodedata
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def normalize_name(name: str) -> str:
    """Normalize a name for comparison: transliterate, strip noise, collapse whitespace."""
    latin = transliterate(name)
    # Remove special chars, hyphens -> spaces
    cleaned = _FTS5_SPECIAL.sub(" ", latin)
    cleaned = cleaned.replace("-", " ")
    # Strip periods, commas, apostrophes (common in names)
    cleaned = cleaned.replace(".", " ").replace(",", " ").replace("'", "")
    # Remove noise tokens
    tokens = cleaned.split()
    tokens = [t for t in tokens if t.lower() not in _NOISE_TOKENS]
    return _MULTI_SPACE.sub(" ", " ".join(tokens)).strip()


# ---------------------------------------------------------------------------
# Phonetic encoding
# ---------------------------------------------------------------------------

def phonetic_encode(name: str) -> tuple[str, str]:
    """Produce metaphone codes for name tokens >= 3 chars.

    Returns (primary_codes_str, alt_codes_str). jellyfish metaphone only
    returns a single code, so alt is always empty.
    """
    tokens = name.lower().split()
    codes = [metaphone(t) for t in tokens if len(t) >= 3]
    return " ".join(codes), ""


# ---------------------------------------------------------------------------
# FTS5 query construction
# ---------------------------------------------------------------------------

def _escape_fts5(text: str) -> str:
    """Strip FTS5 special characters and normalize whitespace."""
    cleaned = _FTS5_SPECIAL.sub(" ", text)
    cleaned = cleaned.replace("-", " ")
    return _MULTI_SPACE.sub(" ", cleaned).strip()


def build_fts5_query(name: str) -> str:
    """Build an FTS5 query that balances precision (phrase match) with recall (OR expansion).

    For single-token names we can only do OR-expanded prefix matching.
    For multi-token names we try an exact phrase first, then fall back to OR tokens.
    """
    cleaned = _escape_fts5(name)
    tokens = [t for t in cleaned.lower().split() if len(t) >= 3]

    if not tokens:
        # Very short name, fall back to prefix match on whatever we have
        fallback = cleaned.lower().strip()
        if not fallback:
            return '""'
        return fallback + "*"

    if len(tokens) == 1:
        # Single meaningful token: prefix match only
        return tokens[0] + "*"

    # Multi-token: phrase match OR individual prefix matches
    phrase = '"' + " ".join(tokens) + '"'
    or_terms = " OR ".join(t + "*" for t in tokens)
    return f"({phrase}) OR ({or_terms})"


# ---------------------------------------------------------------------------
# Stage 1: FTS5 candidate retrieval
# ---------------------------------------------------------------------------

def _fts5_search(
    conn: sqlite3.Connection,
    name_query: str,
    phonetic_query: Optional[str],
    limit: int = 300,
) -> list[dict]:
    """Query sanctions_fts and return raw candidate rows.

    Runs up to two queries (name match + phonetic match) and merges results,
    deduplicating by entity id.
    """
    results: dict[str, dict] = {}  # keyed by entity id

    # Query 1: name/alias match
    if name_query and name_query != '""':
        try:
            rows = conn.execute(
                """SELECT e.*, f.rank
                   FROM sanctions_fts f
                   JOIN sanctions_entities e ON e.rowid = f.rowid
                   WHERE sanctions_fts MATCH ?
                   ORDER BY f.rank
                   LIMIT ?""",
                (name_query, limit),
            ).fetchall()
            for row in rows:
                d = dict(row)
                eid = d.get("id")
                if eid and eid not in results:
                    results[eid] = d
        except sqlite3.OperationalError:
            pass  # bad FTS5 syntax, skip this query

    # Query 2: phonetic match (scoped to phonetic columns)
    if phonetic_query and phonetic_query.strip():
        phonetic_fts = build_fts5_query(phonetic_query)
        if phonetic_fts and phonetic_fts != '""':
            scoped = "{phonetic_primary phonetic_alt}: " + phonetic_fts
            try:
                rows = conn.execute(
                    """SELECT e.*, f.rank
                       FROM sanctions_fts f
                       JOIN sanctions_entities e ON e.rowid = f.rowid
                       WHERE sanctions_fts MATCH ?
                       ORDER BY f.rank
                       LIMIT ?""",
                    (scoped, limit),
                ).fetchall()
                for row in rows:
                    d = dict(row)
                    eid = d.get("id")
                    if eid and eid not in results:
                        results[eid] = d
            except sqlite3.OperationalError:
                pass

    return list(results.values())[:limit]


# ---------------------------------------------------------------------------
# Stage 2: rapidfuzz re-ranking
# ---------------------------------------------------------------------------

def _compute_fuzzy_score(user_name: str, candidate: dict) -> float:
    """Compute best fuzzy score across all name variants for a candidate.

    Uses a weighted blend of token_sort_ratio, token_set_ratio, and WRatio.
    """
    user_norm = normalize_name(user_name)

    candidate_names = [candidate.get("name", "")]
    # Add Latin and ASCII transliterations
    if candidate.get("name_latin"):
        candidate_names.append(candidate["name_latin"])
    if candidate.get("name_ascii"):
        candidate_names.append(candidate["name_ascii"])
    # Add aliases (pipe-separated)
    if candidate.get("aliases"):
        candidate_names.extend(candidate["aliases"].split("|"))

    best_score = 0.0
    best_name = ""

    for cname in candidate_names:
        if not cname:
            continue
        cname_norm = normalize_name(cname)
        if not cname_norm:
            continue

        # Three complementary fuzzy metrics
        sort_score = fuzz.token_sort_ratio(user_norm, cname_norm)
        set_score = fuzz.token_set_ratio(user_norm, cname_norm)
        w_score = fuzz.WRatio(user_norm, cname_norm)

        # Weighted blend: token_set catches subset matches, WRatio is the
        # most forgiving, token_sort is the most balanced.
        combined = (sort_score * 0.35) + (set_score * 0.35) + (w_score * 0.30)

        if combined > best_score:
            best_score = combined
            best_name = cname

    return best_score


def _parse_identifiers(raw: str) -> list[dict]:
    """Parse JSON identifiers string from DB."""
    if not raw:
        return []
    try:
        import json
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def _row_to_candidate(row: dict, score: float) -> dict:
    """Convert a DB row + fuzzy score into a candidate dict for downstream use."""
    return {
        "id": row.get("id", ""),
        "source": row.get("source", ""),
        "list_name": row.get("list_name", ""),
        "name": row.get("name", ""),
        "names": [row.get("name", "")]
                 + ([row["name_latin"]] if row.get("name_latin") else [])
                 + ([row["name_ascii"]] if row.get("name_ascii") else [])
                 + (row.get("aliases", "").split("|") if row.get("aliases") else []),
        "dob": row.get("dob", "").split("|") if row.get("dob") else [],
        "dob_approximate": bool(row.get("dob_approximate", 0)),
        "pob": row.get("pob", "").split("|") if row.get("pob") else [],
        "nationality": row.get("nationality", "").split("|") if row.get("nationality") else [],
        "gender": row.get("gender"),
        "identifiers": _parse_identifiers(row.get("identifiers", "")),
        "addresses": row.get("addresses", "").split("|") if row.get("addresses") else [],
        "designation": row.get("designation"),
        "listing_reason": row.get("listing_reason"),
        "listed_on": row.get("listed_on"),
        "programs": row.get("programs", "").split("|") if row.get("programs") else [],
        "source_url": row.get("source_url", ""),
        "score": round(score, 2),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_candidates(
    user_name: str,
    conn: sqlite3.Connection,
    threshold: int = 60,
    max_results: int = 200,
) -> list[dict]:
    """Find sanctions candidates matching a user name.

    Two-stage process:
      1. FTS5 retrieval: fast full-text search with name + phonetic queries (LIMIT 300)
      2. rapidfuzz re-rank: score every FTS5 hit, drop below threshold, sort descending

    Args:
        user_name: The name to screen (any script).
        conn: sqlite3 connection to the sanctions index database.
        threshold: Minimum fuzzy score (0-100) to keep a candidate. Default 60.
        max_results: Maximum candidates to return. Default 200.

    Returns:
        List of candidate dicts sorted by descending score, each containing
        entity fields plus a 'score' key.
    """
    if not user_name or not user_name.strip():
        return []

    # Prepare queries
    normalized = normalize_name(user_name)
    name_query = build_fts5_query(normalized)

    phonetic_primary, _ = phonetic_encode(normalized)

    # Stage 1: FTS5 retrieval
    raw_candidates = _fts5_search(conn, name_query, phonetic_primary, limit=300)

    if not raw_candidates:
        return []

    # Stage 2: rapidfuzz re-ranking
    scored = []
    for row in raw_candidates:
        score = _compute_fuzzy_score(user_name, row)
        if score >= threshold:
            scored.append(_row_to_candidate(row, score))

    # Sort by score descending, take top max_results
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:max_results]
