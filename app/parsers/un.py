"""Parse UN Security Council Consolidated Sanctions List XML."""

import logging
from lxml import etree

from ..schema import ListEntry

logger = logging.getLogger(__name__)

SOURCE = "un_consolidated"
LIST_NAME = "UN Security Council Consolidated List"
SOURCE_URL = "https://scsanctions.un.org/"
FETCH_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"

ALIAS_QUALITY_MAP = {"Good": "strong", "Low": "weak"}


def _text(el: etree._Element, tag: str) -> str:
    """Get text of a direct child element, or empty string."""
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _build_name(ind: etree._Element) -> str:
    """Concatenate FIRST_NAME through FOURTH_NAME."""
    parts = []
    for tag in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME"):
        val = _text(ind, tag)
        if val:
            parts.append(val)
    return " ".join(parts)


def _parse_aliases(ind: etree._Element) -> tuple[list[str], list[str]]:
    """Parse INDIVIDUAL_ALIAS elements. Returns (names, quality_labels)."""
    names: list[str] = []
    qualities: list[str] = []
    for alias in ind.findall("INDIVIDUAL_ALIAS"):
        name = _text(alias, "ALIAS_NAME")
        if not name:
            continue
        quality_raw = _text(alias, "QUALITY")
        names.append(name)
        qualities.append(ALIAS_QUALITY_MAP.get(quality_raw, "unknown"))
    return names, qualities


def _parse_dobs(ind: etree._Element) -> tuple[list[str], bool]:
    """Parse INDIVIDUAL_DATE_OF_BIRTH elements. Returns (dob_list, is_approximate)."""
    dobs: list[str] = []
    approximate = False
    for dob_el in ind.findall("INDIVIDUAL_DATE_OF_BIRTH"):
        type_of_date = _text(dob_el, "TYPE_OF_DATE")
        date_val = _text(dob_el, "DATE")
        year_val = _text(dob_el, "YEAR")
        from_year = _text(dob_el, "FROM_YEAR")
        to_year = _text(dob_el, "TO_YEAR")

        if type_of_date in ("APPROXIMATELY", "BETWEEN"):
            approximate = True

        if date_val:
            dobs.append(date_val)
        elif year_val:
            dobs.append(year_val)
        elif from_year and to_year:
            dobs.append(f"{from_year}-{to_year}")
        elif from_year:
            dobs.append(from_year)
    return dobs, approximate


def parse_un(xml_path: str) -> list[ListEntry]:
    """Parse UN Consolidated Sanctions XML into ListEntry objects."""
    tree = etree.parse(xml_path)
    root = tree.getroot()

    individuals_section = root.find("INDIVIDUALS")
    if individuals_section is None:
        logger.warning("No <INDIVIDUALS> section found in UN XML")
        return []

    entries: list[ListEntry] = []
    for ind in individuals_section.findall("INDIVIDUAL"):
        try:
            ref = _text(ind, "REFERENCE_NUMBER")
            primary_name = _build_name(ind)
            if not primary_name:
                continue

            names = [primary_name]
            alias_quality = ["strong"]  # primary name is always strong

            orig_script = _text(ind, "NAME_ORIGINAL_SCRIPT")
            if orig_script:
                names.append(orig_script)
                alias_quality.append("strong")

            alias_names, alias_quals = _parse_aliases(ind)
            names.extend(alias_names)
            alias_quality.extend(alias_quals)

            dobs, dob_approx = _parse_dobs(ind)

            pobs = []
            for pob_el in ind.findall("INDIVIDUAL_PLACE_OF_BIRTH"):
                parts = []
                for tag in ("CITY", "STATE_PROVINCE", "COUNTRY"):
                    v = _text(pob_el, tag)
                    if v:
                        parts.append(v)
                if parts:
                    pobs.append(", ".join(parts))

            nationality: list[str] = []
            for nat_el in ind.findall("NATIONALITY/VALUE"):
                if nat_el.text and nat_el.text.strip():
                    nationality.append(nat_el.text.strip())

            identifiers: list[dict] = []
            for doc in ind.findall("INDIVIDUAL_DOCUMENT"):
                identifiers.append({
                    "type": _text(doc, "TYPE_OF_DOCUMENT"),
                    "value": _text(doc, "NUMBER"),
                    "country": _text(doc, "ISSUING_COUNTRY"),
                })

            addresses: list[str] = []
            for addr in ind.findall("INDIVIDUAL_ADDRESS"):
                parts = []
                for tag in ("STREET", "CITY", "STATE_PROVINCE", "COUNTRY"):
                    v = _text(addr, tag)
                    if v:
                        parts.append(v)
                if parts:
                    addresses.append(", ".join(parts))

            gender_raw = _text(ind, "GENDER")
            gender = gender_raw.lower() if gender_raw else None

            entries.append(ListEntry(
                id=f"un-{ref}" if ref else f"un-{primary_name}",
                source=SOURCE,
                list_name=LIST_NAME,
                names=names,
                alias_quality=alias_quality,
                dob=dobs,
                dob_approximate=dob_approx,
                pob=pobs,
                nationality=nationality,
                gender=gender,
                identifiers=identifiers,
                addresses=addresses,
                designation=_text(ind, "DESIGNATION") or None,
                listing_reason=_text(ind, "COMMENTS1") or None,
                listed_on=_text(ind, "LISTED_ON") or None,
                programs=[_text(ind, "UN_LIST_TYPE")] if _text(ind, "UN_LIST_TYPE") else [],
                source_url=SOURCE_URL,
            ))
        except Exception:
            logger.exception("Failed to parse UN individual record")
            continue

    logger.info("Parsed %d individuals from UN Consolidated List", len(entries))
    return entries
