"""Fetch Politically Exposed Persons (PEPs) from Wikidata SPARQL endpoint."""

import logging
import time

import httpx

from ..schema import ListEntry

logger = logging.getLogger(__name__)

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
USER_AGENT = "AML-Discounter/1.0 (brandon@zarpay.app)"

# Key countries: Pakistan, US, UK, UAE, India, Saudi Arabia, Nigeria, Bangladesh
DEFAULT_COUNTRIES = {
    "PK": "Q843",
    "US": "Q30",
    "UK": "Q145",
    "AE": "Q878",
    "IN": "Q668",
    "SA": "Q851",
    "NG": "Q1033",
    "BD": "Q902",
}

SPARQL_TEMPLATE = """
SELECT DISTINCT ?person ?personLabel ?positionLabel ?pepTypeLabel ?startDate ?endDate
WHERE {{
  ?person wdt:P31 wd:Q5 .
  ?person p:P39 ?stmt .
  ?stmt ps:P39 ?position .
  ?position wdt:P279 ?pepType .
  VALUES ?pepType {{ wd:Q48352 wd:Q2285706 wd:Q83307 wd:Q486839 wd:Q4175034 wd:Q16533 wd:Q132050 wd:Q121998 wd:Q15686806 }}
  {{ ?position wdt:P1001 ?country . }} UNION {{ ?position wdt:P17 ?country . }}
  FILTER (?country = wd:{qid})
  OPTIONAL {{ ?stmt pq:P580 ?startDate . }}
  OPTIONAL {{ ?stmt pq:P582 ?endDate . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}}
ORDER BY ?personLabel
"""


def fetch_wikidata_peps(countries: list[str] = None) -> list[ListEntry]:
    """Fetch PEPs from Wikidata, querying per-country to avoid timeouts.

    Args:
        countries: List of ISO 2-letter country codes. Defaults to all key countries.

    Returns:
        List of ListEntry objects for each PEP found.
    """
    if countries is None:
        country_map = DEFAULT_COUNTRIES
    else:
        country_map = {c: DEFAULT_COUNTRIES[c] for c in countries if c in DEFAULT_COUNTRIES}

    entries: list[ListEntry] = []
    seen_qids: set[str] = set()

    for iso_code, qid in country_map.items():
        logger.info("Fetching PEPs for %s (wd:%s)", iso_code, qid)
        query = SPARQL_TEMPLATE.format(qid=qid)
        try:
            resp = httpx.get(
                WIKIDATA_SPARQL,
                params={"query": query, "format": "json"},
                headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
                timeout=90,
            )
            if resp.status_code >= 500:
                logger.warning("Wikidata returned %d for %s, skipping this country", resp.status_code, iso_code)
                time.sleep(2)
                continue
            resp.raise_for_status()
            results = resp.json().get("results", {}).get("bindings", [])
            logger.info("  %s: %d results", iso_code, len(results))

            for row in results:
                try:
                    person_uri = row.get("person", {}).get("value", "")
                    person_qid = person_uri.rsplit("/", 1)[-1] if person_uri else ""
                    if not person_qid or person_qid in seen_qids:
                        continue
                    seen_qids.add(person_qid)

                    name = row.get("personLabel", {}).get("value", "").strip()
                    if not name or name == person_qid:
                        continue

                    position = row.get("positionLabel", {}).get("value", "")
                    start = row.get("startDate", {}).get("value", "")[:10] if row.get("startDate") else ""
                    end = row.get("endDate", {}).get("value", "")[:10] if row.get("endDate") else ""

                    entries.append(ListEntry(
                        id=f"wd-{person_qid}",
                        source="wikidata_peps",
                        list_name=f"Wikidata PEPs ({iso_code})",
                        names=[name],
                        nationality=[iso_code],
                        designation=position or None,
                        listed_on=start or None,
                        source_url=person_uri,
                        raw={"position": position, "start": start, "end": end, "country": iso_code},
                    ))
                except Exception:
                    logger.warning("Skipping bad PEP record: %s", row, exc_info=True)

        except Exception:
            logger.warning("Failed to fetch PEPs for %s", iso_code, exc_info=True)

        time.sleep(2)  # Rate limit between country queries

    logger.info("Total unique PEPs fetched: %d", len(entries))
    return entries
