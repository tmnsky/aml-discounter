"""Unified entity schema for sanctions/PEP screening."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ListEntry:
    """A single individual from a sanctions/PEP list."""

    id: str  # source-prefixed: "ofac-12345", "un-QDi.137", "wd-Q1234"
    source: str  # "ofac_sdn", "un_consolidated", "eu_fsf", etc.
    list_name: str  # "OFAC SDN List", "UN Security Council 1267/1989/2253"
    names: list[str]  # primary name + all aliases + transliteration variants
    alias_quality: list[str] = field(
        default_factory=list
    )  # per-name: "strong", "weak", "unknown"
    dob: list[str] = field(default_factory=list)  # ISO dates, year-only, or ranges
    dob_approximate: bool = False
    pob: list[str] = field(default_factory=list)
    nationality: list[str] = field(default_factory=list)  # ISO country codes
    gender: Optional[str] = None  # "male", "female", None
    identifiers: list[dict] = field(
        default_factory=list
    )  # [{type, value, country}, ...]
    addresses: list[str] = field(default_factory=list)
    designation: Optional[str] = None  # "Taliban deputy minister"
    listing_reason: Optional[str] = None
    listed_on: Optional[str] = None  # ISO date
    programs: list[str] = field(default_factory=list)  # ["SDGT", "NPWMD"]
    source_url: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class DeduplicatedMatch:
    """A unique person after cross-list deduplication."""

    representative: ListEntry  # richest record
    all_sources: list[dict] = field(
        default_factory=list
    )  # [{source, list_name, source_url, listed_on, programs}]
    all_names: list[str] = field(default_factory=list)
    all_identifiers: list[dict] = field(default_factory=list)
    uncertain_merge: bool = False  # True if merged with no shared DOB/ID


@dataclass
class MatchDecision:
    """Claude's decision on a single match."""

    match_number: int
    decision: str  # "CLEARED", "LIKELY_MATCH", "ESCALATE"
    contradictions: list[dict] = field(default_factory=list)
    supporting_similarities: list[dict] = field(default_factory=list)
    reasoning: str = ""
    cleared_by: str = ""  # "rule:gender", "rule:dob", "rule:id", "ai"


@dataclass
class ScreeningResult:
    """Full result of a screening."""

    id: str  # "SCR-20260414-000347"
    timestamp: str
    user_input: dict
    result: str  # "CLEAR", "FLAG", "ESCALATE"
    raw_candidates: int
    unique_persons: int
    auto_cleared: int
    auto_flagged: int
    sent_to_llm: int
    llm_cleared: int
    llm_flagged: int
    llm_escalated: int
    investigations_run: int = 0  # Pass 2 Perplexity investigations
    matches: list[dict] = field(default_factory=list)  # all matches with decisions
    llm_calls: list[dict] = field(default_factory=list)  # Claude I/O for audit
    investigation_audits: list[dict] = field(default_factory=list)  # Pass 2 investigation details
    source_versions: dict = field(default_factory=dict)  # {source: sha256}
    processing_ms: int = 0
    screened_by: str = ""
