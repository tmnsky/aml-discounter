"""Parse Switzerland SECO (SESAM) Sanctions XML."""

import logging
from lxml import etree

from ..schema import ListEntry

logger = logging.getLogger(__name__)

SOURCE = "ch_seco"
LIST_NAME = "Switzerland SECO Sanctions List"
SOURCE_URL = "https://www.seco.admin.ch/seco/en/home/Aussenwirtschaftspolitik_Wirtschaftliche_Zusammenarbeit/Wirtschaftsbeziehungen/Exportkontrollen-und-Sanktionen/Sanktionen-Embargos.html"
FETCH_URL = "https://www.sesam.search.admin.ch/sesam-search-web/pages/downloadXmlGesamtliste.xhtml?lang=en&action=downloadXmlGesamtlisteAction"

# Name part type mapping
NAME_TYPE_MAP = {
    "given-name": "given",
    "family-name": "family",
    "father-name": "father",
    "maiden-name": "maiden",
    "tribal-name": "tribal",
    "whole-name": "whole",
    "other": "other",
}


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


def _el_text(el: etree._Element | None) -> str:
    """Get text of an element, or empty string."""
    if el is None:
        return ""
    return (el.text or "").strip()


def _build_name_from_parts(name_parts: list[tuple[str, str]]) -> str:
    """Build a full name from typed name parts.

    Args:
        name_parts: list of (type, value) tuples
    """
    given_parts = []
    family_parts = []
    other_parts = []

    for part_type, value in name_parts:
        if part_type in ("given", "other"):
            given_parts.append(value)
        elif part_type in ("family", "maiden", "tribal"):
            family_parts.append(value)
        elif part_type == "father":
            given_parts.append(value)
        elif part_type == "whole":
            return value  # whole-name is already complete
        else:
            other_parts.append(value)

    parts = given_parts + family_parts + other_parts
    return " ".join(parts)


def _parse_names(individual: etree._Element) -> tuple[list[str], list[str]]:
    """Parse identity/name elements with spelling variants.

    Returns (names, alias_quality).
    """
    names: list[str] = []
    qualities: list[str] = []
    seen: set[str] = set()

    for identity in individual.findall(".//identity"):
        is_primary = identity.get("main", "").lower() == "true"

        for name_el in identity.findall("name"):
            # Each name can have multiple spelling variants
            for spelling in name_el.findall("spelling-variant"):
                name_parts: list[tuple[str, str]] = []

                for part in spelling.findall("name-part"):
                    part_type_raw = part.get("spelling-variant-type", "") or part.get("type", "")
                    # Also check parent name-part-group for type
                    group = part.getparent()
                    if not part_type_raw and group is not None:
                        part_type_raw = group.get("type", "")

                    part_type = NAME_TYPE_MAP.get(part_type_raw, "other")
                    value = _el_text(part)
                    if value:
                        name_parts.append((part_type, value))

                full_name = _build_name_from_parts(name_parts)
                if not full_name:
                    continue

                norm = full_name.lower()
                if norm in seen:
                    continue
                seen.add(norm)

                names.append(full_name)
                qualities.append("strong" if is_primary else "weak")

            # If no spelling variants, try name-part elements directly
            if not name_el.findall("spelling-variant"):
                name_parts = []
                for part_group in name_el.findall("name-part-group"):
                    group_type = part_group.get("type", "")
                    mapped = NAME_TYPE_MAP.get(group_type, "other")

                    for part in part_group.findall("name-part"):
                        value = _el_text(part)
                        if value:
                            name_parts.append((mapped, value))

                # Also check direct name-part children
                for part in name_el.findall("name-part"):
                    part_type_raw = part.get("type", "")
                    mapped = NAME_TYPE_MAP.get(part_type_raw, "other")
                    value = _el_text(part)
                    if value:
                        name_parts.append((mapped, value))

                full_name = _build_name_from_parts(name_parts)
                if not full_name:
                    continue

                norm = full_name.lower()
                if norm in seen:
                    continue
                seen.add(norm)

                names.append(full_name)
                qualities.append("strong" if is_primary else "weak")

    return names, qualities


def _parse_dobs(individual: etree._Element) -> tuple[list[str], bool]:
    """Parse date-of-birth elements. Returns (dob_list, is_approximate)."""
    dobs: list[str] = []
    approximate = False

    for dob_el in individual.findall(".//date-of-birth"):
        # Try different child elements
        date_val = _text(dob_el, "date")
        year_val = _text(dob_el, "year")
        from_year = _text(dob_el, "from-year")
        to_year = _text(dob_el, "to-year")
        approx = dob_el.get("approximate", "").lower() == "true"

        if approx:
            approximate = True

        if date_val:
            dobs.append(date_val)
        elif year_val:
            dobs.append(year_val)
        elif from_year and to_year:
            dobs.append(f"{from_year}-{to_year}")
            approximate = True
        elif from_year:
            dobs.append(from_year)
            approximate = True

    return dobs, approximate


def parse_switzerland(xml_path: str) -> list[ListEntry]:
    """Parse Switzerland SECO Sanctions XML into ListEntry objects."""
    tree = etree.parse(xml_path)
    _strip_ns(tree)
    root = tree.getroot()

    entries: list[ListEntry] = []
    for target in root.iter("target"):
        try:
            # Check for de-listed entries
            mod_type = target.get("modification-type", "")
            if mod_type == "de-listed":
                continue

            # Must have an <individual> child
            individual = target.find("individual")
            if individual is None:
                continue

            target_id = target.get("ssid", "") or target.get("id", "")

            names, alias_quality = _parse_names(individual)
            if not names:
                continue

            dobs, dob_approx = _parse_dobs(individual)

            # Gender
            gender_raw = _text(individual, "sex")
            gender = None
            if gender_raw:
                gender = gender_raw.lower()
                if gender not in ("male", "female"):
                    gender = None

            # Place of birth
            pobs: list[str] = []
            for pob_el in individual.findall(".//place-of-birth"):
                parts = []
                city = _text(pob_el, "city")
                country = _text(pob_el, "country")
                if city:
                    parts.append(city)
                if country:
                    parts.append(country)
                if parts:
                    pobs.append(", ".join(parts))

            # Nationality
            nationality: list[str] = []
            for nat_el in individual.findall(".//nationality"):
                country = _text(nat_el, "country")
                if not country:
                    country = _el_text(nat_el)
                if country:
                    nationality.append(country)

            # Identification documents
            identifiers: list[dict] = []
            for doc in individual.findall(".//identification-document"):
                doc_type = _text(doc, "type")
                number = _text(doc, "number")
                country = _text(doc, "issuing-country")
                if number:
                    identifiers.append({
                        "type": doc_type,
                        "value": number,
                        "country": country,
                    })

            # Addresses
            addresses: list[str] = []
            for addr in individual.findall(".//address"):
                parts = []
                for tag in ("street", "city", "zip-code", "country"):
                    v = _text(addr, tag)
                    if v:
                        parts.append(v)
                if parts:
                    addresses.append(", ".join(parts))

            # Programs / sanctions program
            programs: list[str] = []
            for prog_el in target.findall(".//sanctions-program"):
                prog_name = prog_el.get("name", "") or _el_text(prog_el)
                if prog_name and prog_name not in programs:
                    programs.append(prog_name)

            # Justification / listing reason
            listing_reason = None
            justification = target.find(".//justification")
            if justification is not None:
                listing_reason = _el_text(justification) or None

            # Listed on date
            listed_on = target.get("listed-on", "") or None

            entries.append(ListEntry(
                id=f"ch-{target_id}" if target_id else f"ch-{names[0]}",
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
                listing_reason=listing_reason,
                listed_on=listed_on,
                programs=programs,
                source_url=SOURCE_URL,
            ))
        except Exception:
            logger.exception("Failed to parse SECO target record")
            continue

    logger.info("Parsed %d individuals from SECO Sanctions List", len(entries))
    return entries
