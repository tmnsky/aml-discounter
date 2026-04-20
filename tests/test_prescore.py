"""Tests for the deterministic pre-score filter, especially the temporal impossibility rule."""

from __future__ import annotations

from app.prescore import prescore, _temporal_impossibility, _extract_years
from app.schema import DeduplicatedMatch, ListEntry


def _make_match(**kwargs) -> DeduplicatedMatch:
    defaults = dict(
        id="m1",
        source="un_consolidated",
        list_name="UN SC",
        names=["Test Person"],
    )
    defaults.update(kwargs)
    rep = ListEntry(**defaults)
    return DeduplicatedMatch(
        representative=rep,
        all_sources=[{
            "source": rep.source,
            "list_name": rep.list_name,
            "source_url": "",
            "listed_on": rep.listed_on,
            "programs": rep.programs,
        }],
        all_names=rep.names,
        all_identifiers=rep.identifiers,
    )


def test_extract_years():
    assert _extract_years(["1985-03-12"]) == [1985]
    assert _extract_years(["1985"]) == [1985]
    assert _extract_years(["circa 1970"]) == []  # Not a leading 4-digit year
    assert _extract_years(["2003-08-31", "1990"]) == [2003, 1990]
    assert _extract_years(["not a date"]) == []


def test_temporal_impossibility_customer_born_after_listing():
    """Customer born 2003, sanctioned person listed in 2001 → impossible."""
    assert _temporal_impossibility(
        user_dobs=["2003-08-31"],
        listed_on="2001-10-17",
    ) is True


def test_temporal_impossibility_customer_old_enough():
    """Customer born 1970, sanctioned person listed in 2001 → possible."""
    assert _temporal_impossibility(
        user_dobs=["1970-01-01"],
        listed_on="2001-10-17",
    ) is False


def test_temporal_impossibility_customer_young_but_adult_at_listing():
    """Customer born 1985, listed 2001 → would have been 16, plausible (threshold is 15)."""
    assert _temporal_impossibility(
        user_dobs=["1985-01-01"],
        listed_on="2001-10-17",
    ) is False


def test_temporal_impossibility_no_data():
    """If either field is missing, can't prove impossibility."""
    assert _temporal_impossibility(user_dobs=[], listed_on="2001") is False
    assert _temporal_impossibility(user_dobs=["2003"], listed_on=None) is False


def test_prescore_temporal_rule_clears_abdul_manan_case():
    """Abdul Manan case: customer born 2003, matched to Al-Qaida listed 2001."""
    user = {
        "name": "Abdul Manan",
        "dob": "2003-08-31",
        "gender": "Male",
        "nationality": "Pakistan",
        "identifiers": [],
    }
    match = _make_match(
        names=["Abdul Manan Agha"],
        listed_on="2001-10-17",
        programs=["Al-Qaida"],
    )
    cleared, flagged, send_to_llm = prescore(user, [match])
    assert len(cleared) == 1
    assert len(flagged) == 0
    assert len(send_to_llm) == 0
    assert "temporal" in cleared[0]["cleared_by"]


def test_prescore_gender_conflict_clears():
    user = {"name": "X", "gender": "Male", "dob": "1990", "identifiers": []}
    match = _make_match(names=["X"], gender="Female")
    cleared, _, _ = prescore(user, [match])
    assert len(cleared) == 1
    assert cleared[0]["cleared_by"] == "rule:gender"


def test_prescore_dob_gap_clears():
    user = {"name": "X", "dob": "1995", "identifiers": []}
    match = _make_match(names=["X"], dob=["1950"])
    cleared, _, _ = prescore(user, [match])
    assert len(cleared) == 1
    assert cleared[0]["cleared_by"] == "rule:dob"


def test_prescore_id_match_flags():
    user = {
        "name": "X",
        "identifiers": [{"type": "cnic", "value": "35202-5030579-1"}],
    }
    match = _make_match(
        names=["X"],
        identifiers=[{"type": "cnic", "value": "35202-5030579-1", "country": "PK"}],
    )
    _, flagged, _ = prescore(user, [match])
    assert len(flagged) == 1
    assert flagged[0]["flagged_by"] == "rule:id"


def test_prescore_ambiguous_goes_to_llm():
    user = {"name": "X", "dob": "", "identifiers": []}
    match = _make_match(names=["X"])
    _, _, send_to_llm = prescore(user, [match])
    assert len(send_to_llm) == 1
