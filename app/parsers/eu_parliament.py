"""Parse European Parliament MEPs from the official XML feed."""

import logging

import httpx
from lxml import etree

from ..schema import ListEntry

logger = logging.getLogger(__name__)

MEP_XML_URL = "https://www.europarl.europa.eu/meps/en/full-list/xml"


def fetch_eu_parliament() -> list[ListEntry]:
    """Fetch all current MEPs from the European Parliament XML feed."""
    resp = httpx.get(MEP_XML_URL, headers={"User-Agent": "AML-Discounter/1.0"}, timeout=60)
    resp.raise_for_status()
    root = etree.fromstring(resp.content)
    entries: list[ListEntry] = []

    for mep in root.findall(".//mep"):
        try:
            full_name = (mep.findtext("fullName") or "").strip()
            if not full_name:
                continue

            mep_id = (mep.findtext("id") or "").strip()
            country = (mep.findtext("country") or "").strip()
            group = (mep.findtext("politicalGroup") or "").strip()

            entries.append(ListEntry(
                id=f"eup-{mep_id}" if mep_id else f"eup-{full_name.replace(' ', '_')}",
                source="eu_parliament",
                list_name="European Parliament (Current MEPs)",
                names=[full_name],
                nationality=[country] if country else [],
                designation=group or None,
                source_url=f"https://www.europarl.europa.eu/meps/en/{mep_id}" if mep_id else MEP_XML_URL,
                raw={"country": country, "political_group": group},
            ))
        except Exception:
            logger.warning("Skipping bad MEP record", exc_info=True)

    logger.info("EU Parliament MEPs parsed: %d", len(entries))
    return entries
