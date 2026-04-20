"""Parse UK FCDO Sanctions List XML."""

import logging
import re
from lxml import etree

from ..schema import ListEntry

logger = logging.getLogger(__name__)

SOURCE = "uk_sanctions"
LIST_NAME = "UK Sanctions List"
SOURCE_URL = "https://sanctionslist.fcdo.gov.uk/"
FETCH_URL = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml"


def _strip_ns(tree: etree._ElementTree) -> None:
    """Remove namespace prefixes from all elements in-place."""
    for el in tree.iter():
        if isinstance(el.tag, str) and el.tag.startswith("{"):
            el.tag = el.tag.split("}", 1)[1]
        new_attrib = {}
        for k, v in el.attrib.items():
            if k.startswith("{"):
                k = k.split("}", 1)[1]
            new_attrib[k] = v
        el.attrib.clear()
        el.attrib.update(new_attrib)


def _text(el: etree._Element, tag: str) -> str:
    """Get text of a direct child element, or empty string."""
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _build_name_from_parts(name_el: etree._Element) -> str:
    """Build a full name from Name1-Name6 scheme.

    Name1 = given name, Name6 = family name. Name2-Name5 are middle/other parts.
    """
    given_parts = []
    for i in range(1, 6):
        val = _text(name_el, f"Name{i}")
        if val:
            given_parts.append(val)
    family = _text(name_el, "Name6")

    parts = given_parts + ([family] if family else [])
    return " ".join(parts)


ALIAS_STRENGTH_MAP = {
    "good quality": "strong",
    "low quality": "weak",
}


def _parse_names(designation: etree._Element) -> tuple[list[str], list[str]]:
    """Parse Names section with NameType and AliasStrength.

    Returns (names, alias_quality).
    """
    names: list[str] = []
    qualities: list[str] = []
    seen: set[str] = set()

    for name_el in designation.findall(".//Name"):
        name_type = _text(name_el, "NameType")
        alias_strength = _text(name_el, "AliasStrength").lower()
        full_name = _build_name_from_parts(name_el)

        if not full_name:
            continue

        norm = full_name.lower()
        if norm in seen:
            continue
        seen.add(norm)

        # Determine quality
        if name_type == "Primary Name":
            quality = "strong"
        elif name_type == "Primary Name Variation":
            quality = "strong"
        elif name_type == "Alias":
            quality = ALIAS_STRENGTH_MAP.get(alias_strength, "unknown")
        else:
            quality = "unknown"

        names.append(full_name)
        qualities.append(quality)

    return names, qualities


def _parse_dobs(designation: etree._Element) -> tuple[list[str], bool]:
    """Parse DOB elements. Returns (dob_list, is_approximate)."""
    dobs: list[str] = []
    approximate = False

    for dob_el in designation.findall(".//IndividualDateOfBirth"):
        # Full date
        val = _text(dob_el, "DateOfBirth")
        if val:
            dobs.append(val)
            continue

        # Year only
        year = _text(dob_el, "Year")
        if year:
            dobs.append(year)
            approximate = True
            continue

    # Also check DOB elements directly
    for dob_el in designation.findall(".//DOB"):
        val = (dob_el.text or "").strip()
        if val:
            dobs.append(val)

    return dobs, approximate


def parse_uk(xml_path: str) -> list[ListEntry]:
    """Parse UK Sanctions List XML into ListEntry objects."""
    tree = etree.parse(xml_path)
    _strip_ns(tree)
    root = tree.getroot()

    entries: list[ListEntry] = []
    for desig in root.iter("Designation"):
        try:
            # Filter to individuals only
            entity_type = _text(desig, "IndividualEntityShip")
            if entity_type != "Individual":
                continue

            unique_id = _text(desig, "UniqueID")

            names, alias_quality = _parse_names(desig)
            if not names:
                continue

            dobs, dob_approx = _parse_dobs(desig)

            # Gender
            gender_raw = _text(desig, "Gender")
            gender = gender_raw.lower() if gender_raw else None

            # Nationalities
            nationality: list[str] = []
            for nat_el in desig.findall(".//Nationality"):
                val = (nat_el.text or "").strip()
                if val:
                    nationality.append(val)

            # Place of birth
            pobs: list[str] = []
            for pob_el in desig.findall(".//TownOfBirth"):
                val = (pob_el.text or "").strip()
                if val:
                    pobs.append(val)
            for pob_el in desig.findall(".//CountryOfBirth"):
                val = (pob_el.text or "").strip()
                if val and val not in pobs:
                    pobs.append(val)

            # Passport / identifiers
            identifiers: list[dict] = []
            for pp in desig.findall(".//PassportDetails"):
                number = _text(pp, "PassportNumber")
                country = _text(pp, "PassportCountry")
                if number:
                    identifiers.append({
                        "type": "passport",
                        "value": number,
                        "country": country,
                    })

            # Addresses
            addresses: list[str] = []
            for addr in desig.findall(".//Address"):
                parts = []
                for child in addr:
                    val = (child.text or "").strip()
                    if val:
                        parts.append(val)
                if parts:
                    addresses.append(", ".join(parts))

            # Programs / regime
            programs: list[str] = []
            regime = _text(desig, "RegimeName")
            if regime:
                programs.append(regime)

            # Listing reason
            listing_reason = _text(desig, "OtherInformation") or None

            # Listed on date
            listed_on = _text(desig, "DateListed") or None

            entries.append(ListEntry(
                id=f"uk-{unique_id}" if unique_id else f"uk-{names[0]}",
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
                designation=_text(desig, "Position") or None,
                listing_reason=listing_reason,
                listed_on=listed_on,
                programs=programs,
                source_url=SOURCE_URL,
            ))
        except Exception:
            logger.exception("Failed to parse UK Designation record")
            continue

    logger.info("Parsed %d individuals from UK Sanctions List", len(entries))
    return entries
