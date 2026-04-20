"""Fetch current UK Parliament members from the Members API."""

import logging

import httpx

from ..schema import ListEntry

logger = logging.getLogger(__name__)

BASE_URL = "https://members-api.parliament.uk/api/Members/Search"
PAGE_SIZE = 20


def fetch_uk_parliament() -> list[ListEntry]:
    """Fetch all current UK Parliament members via paginated JSON API."""
    entries: list[ListEntry] = []
    skip = 0

    while True:
        try:
            resp = httpx.get(
                BASE_URL,
                params={"skip": skip, "take": PAGE_SIZE, "IsCurrentMember": "true"},
                headers={"User-Agent": "AML-Discounter/1.0"},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.warning("Failed to fetch UK Parliament page at skip=%d", skip, exc_info=True)
            break

        items = data.get("items", [])
        if not items:
            break

        for item in items:
            try:
                val = item.get("value", {})
                name = val.get("nameDisplayAs", "").strip()
                if not name:
                    continue

                member_id = val.get("id", "")
                gender_raw = val.get("gender", "")
                gender = gender_raw.lower() if gender_raw in ("Male", "Female") else None

                membership = val.get("latestHouseMembership", {})
                house = membership.get("house", "")  # 1=Commons, 2=Lords
                house_name = {1: "House of Commons", 2: "House of Lords"}.get(house, str(house))
                party = membership.get("membershipFrom", "")

                entries.append(ListEntry(
                    id=f"ukp-{member_id}",
                    source="uk_parliament",
                    list_name="UK Parliament (Current Members)",
                    names=[name],
                    nationality=["GB"],
                    gender=gender,
                    designation=f"{house_name} - {party}" if party else house_name,
                    source_url=f"https://members.parliament.uk/member/{member_id}",
                    raw={"house": house, "party": party, "gender": gender_raw},
                ))
            except Exception:
                logger.warning("Skipping bad UK Parliament record: %s", item, exc_info=True)

        total = data.get("totalResults", 0)
        skip += PAGE_SIZE
        if skip >= total:
            break

    logger.info("UK Parliament members parsed: %d", len(entries))
    return entries
