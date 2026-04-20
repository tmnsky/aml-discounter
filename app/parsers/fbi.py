"""Fetch FBI Most Wanted list via the public API."""

import logging

import httpx

from ..schema import ListEntry

logger = logging.getLogger(__name__)

FBI_API_URL = "https://api.fbi.gov/wanted/v1/list"
PAGE_SIZE = 50


def fetch_fbi_wanted() -> list[ListEntry]:
    """Fetch all FBI Most Wanted entries via paginated JSON API."""
    entries: list[ListEntry] = []
    page = 1

    while True:
        try:
            resp = httpx.get(
                FBI_API_URL,
                params={"pageSize": PAGE_SIZE, "page": page},
                headers={"User-Agent": "AML-Discounter/1.0"},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.warning("Failed to fetch FBI page %d", page, exc_info=True)
            break

        items = data.get("items", [])
        if not items:
            break

        for item in items:
            try:
                name = (item.get("title") or "").strip()
                if not name:
                    continue

                uid = item.get("uid", "")

                dobs = item.get("dates_of_birth_used") or []
                dob_list = [d for d in dobs if d]

                nationality_raw = item.get("nationality") or ""
                nationalities = [n.strip() for n in nationality_raw.split(",") if n.strip()] if nationality_raw else []

                sex_raw = (item.get("sex") or "").lower()
                gender = sex_raw if sex_raw in ("male", "female") else None

                description = item.get("description") or ""
                aliases = item.get("aliases") or []
                all_names = [name] + [a for a in aliases if a and a != name]

                entries.append(ListEntry(
                    id=f"fbi-{uid}" if uid else f"fbi-{name.replace(' ', '_')}",
                    source="fbi_wanted",
                    list_name="FBI Most Wanted",
                    names=all_names,
                    dob=dob_list,
                    nationality=nationalities,
                    gender=gender,
                    designation=description or None,
                    source_url=item.get("url") or f"https://www.fbi.gov/wanted/{uid}",
                    raw={"description": description, "subjects": item.get("subjects", [])},
                ))
            except Exception:
                logger.warning("Skipping bad FBI record: %s", item.get("title", ""), exc_info=True)

        total = data.get("total", 0)
        if page * PAGE_SIZE >= total:
            break
        page += 1

    logger.info("FBI Most Wanted entries parsed: %d", len(entries))
    return entries
