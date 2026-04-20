"""Parse current US Congress members from unitedstates/congress-legislators."""

import logging

import httpx
import yaml

from ..schema import ListEntry

logger = logging.getLogger(__name__)

SOURCE_URL = "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/legislators-current.yaml"


def fetch_us_congress() -> list[ListEntry]:
    """Fetch current US Congress members from the congress-legislators YAML."""
    resp = httpx.get(SOURCE_URL, headers={"User-Agent": "AML-Discounter/1.0"}, timeout=60)
    resp.raise_for_status()
    legislators = yaml.safe_load(resp.text)
    entries: list[ListEntry] = []

    for leg in legislators:
        try:
            name_info = leg.get("name", {})
            first = name_info.get("first", "")
            last = name_info.get("last", "")
            full_name = f"{first} {last}".strip()
            if not full_name:
                continue

            bio = leg.get("bio", {})
            birthday = bio.get("birthday", "")
            gender_raw = bio.get("gender", "")
            gender = {"M": "male", "F": "female"}.get(gender_raw)

            ids = leg.get("id", {})
            bioguide = ids.get("bioguide", "")

            terms = leg.get("terms", [])
            latest = terms[-1] if terms else {}
            state = latest.get("state", "")
            party = latest.get("party", "")
            chamber = latest.get("type", "")  # "sen" or "rep"

            entries.append(ListEntry(
                id=f"usc-{bioguide}" if bioguide else f"usc-{full_name.replace(' ', '_')}",
                source="us_congress",
                list_name="US Congress (Current Members)",
                names=[full_name],
                dob=[birthday] if birthday else [],
                nationality=["US"],
                gender=gender,
                designation=f"{chamber.upper()} - {state} ({party})" if chamber else None,
                source_url=f"https://bioguide.congress.gov/search/bio/{bioguide}" if bioguide else SOURCE_URL,
                raw={"state": state, "party": party, "chamber": chamber, "bioguide": bioguide},
            ))
        except Exception:
            logger.warning("Skipping bad congress record: %s", leg.get("name", {}), exc_info=True)

    logger.info("US Congress members parsed: %d", len(entries))
    return entries
