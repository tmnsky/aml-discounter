"""Parse EU Financial Sanctions Files (FSF) XML v1.1."""

import logging
import re
from lxml import etree

from ..schema import ListEntry

logger = logging.getLogger(__name__)

SOURCE = "eu_fsf"
LIST_NAME = "EU Consolidated Financial Sanctions List"
SOURCE_URL = "https://webgate.ec.europa.eu/fsd/fsf"
FETCH_URL = (
    "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content"
    "?token=dG9rZW4tMjAxNw"
)

EU_NS = "http://eu.europa.ec/fpi/fsd/export"


def _strip_ns(tree: etree._ElementTree) -> None:
    """Remove namespace prefixes from all elements in-place."""
    for el in tree.iter():
        if isinstance(el.tag, str) and el.tag.startswith("{"):
            el.tag = el.tag.split("}", 1)[1]
        # Strip namespace from attributes too
        new_attrib = {}
        for k, v in el.attrib.items():
            if k.startswith("{"):
                k = k.split("}", 1)[1]
            new_attrib[k] = v
        el.attrib.clear()
        el.attrib.update(new_attrib)


def _attr(el: etree._Element, name: str) -> str:
    """Get attribute value or empty string."""
    return (el.get(name) or "").strip()


def _parse_names(entity: etree._Element) -> tuple[list[str], list[str], str | None]:
    """Parse nameAlias elements. Returns (names, alias_quality, gender)."""
    names: list[str] = []
    qualities: list[str] = []
    gender = None
    seen: set[str] = set()

    # First pass: get English primary name
    for alias in entity.findall("nameAlias"):
        lang = _attr(alias, "nameLanguage")
        whole = _attr(alias, "wholeName")
        first = _attr(alias, "firstName")
        middle = _attr(alias, "middleName")
        last = _attr(alias, "lastName")

        name = whole or " ".join(p for p in (first, middle, last) if p)
        if not name:
            continue

        # Gender from any name entry
        if not gender:
            g = _attr(alias, "gender")
            if g:
                gender = g.lower()

        is_strong = _attr(alias, "strong").lower() == "true"

        if lang == "EN":
            norm = name.lower()
            if norm not in seen:
                seen.add(norm)
                names.append(name)
                qualities.append("strong")

    # Second pass: strong aliases from non-English languages
    for alias in entity.findall("nameAlias"):
        lang = _attr(alias, "nameLanguage")
        if lang == "EN":
            continue

        is_strong = _attr(alias, "strong").lower() == "true"
        if not is_strong:
            continue

        whole = _attr(alias, "wholeName")
        first = _attr(alias, "firstName")
        middle = _attr(alias, "middleName")
        last = _attr(alias, "lastName")

        name = whole or " ".join(p for p in (first, middle, last) if p)
        if not name:
            continue

        norm = name.lower()
        if norm not in seen:
            seen.add(norm)
            names.append(name)
            qualities.append("strong")

    # Third pass: weak (non-strong, non-English) aliases
    for alias in entity.findall("nameAlias"):
        lang = _attr(alias, "nameLanguage")
        is_strong = _attr(alias, "strong").lower() == "true"
        if lang == "EN" or is_strong:
            continue

        whole = _attr(alias, "wholeName")
        first = _attr(alias, "firstName")
        middle = _attr(alias, "middleName")
        last = _attr(alias, "lastName")

        name = whole or " ".join(p for p in (first, middle, last) if p)
        if not name:
            continue

        norm = name.lower()
        if norm not in seen:
            seen.add(norm)
            names.append(name)
            qualities.append("weak")

    return names, qualities, gender


def _parse_birthdates(entity: etree._Element) -> tuple[list[str], bool]:
    """Parse birthdate elements. Returns (dob_list, is_approximate)."""
    dobs: list[str] = []
    approximate = False
    for bd in entity.findall("birthdate"):
        circa = _attr(bd, "circa").lower() == "true"
        if circa:
            approximate = True

        date_val = _attr(bd, "birthdate")
        year_val = _attr(bd, "year")

        if date_val:
            dobs.append(date_val)
        elif year_val:
            dobs.append(year_val)
    return dobs, approximate


def _parse_pob(entity: etree._Element) -> list[str]:
    """Parse birthdate elements for place of birth."""
    pobs: list[str] = []
    for bd in entity.findall("birthdate"):
        parts = []
        city = _attr(bd, "city")
        country = _attr(bd, "countryIso2Code")
        if city:
            parts.append(city)
        if country:
            parts.append(country)
        if parts:
            pobs.append(", ".join(parts))
    return pobs


def parse_eu(xml_path: str) -> list[ListEntry]:
    """Parse EU Financial Sanctions XML into ListEntry objects."""
    tree = etree.parse(xml_path)
    _strip_ns(tree)
    root = tree.getroot()

    entries: list[ListEntry] = []
    for entity in root.iter("sanctionEntity"):
        try:
            # Filter to persons only
            subject = entity.find("subjectType")
            if subject is None:
                continue
            code = _attr(subject, "code")
            cls_code = _attr(subject, "classificationCode")
            if code != "person" or cls_code != "P":
                continue

            eu_ref = _attr(entity, "euReferenceNumber")

            names, alias_quality, gender = _parse_names(entity)
            if not names:
                continue

            dobs, dob_approx = _parse_birthdates(entity)
            pobs = _parse_pob(entity)

            # Citizenship / nationality
            nationality: list[str] = []
            for cit in entity.findall("citizenship"):
                cc = _attr(cit, "countryIso2Code")
                if cc:
                    nationality.append(cc)

            # Identification documents
            identifiers: list[dict] = []
            for ident in entity.findall("identification"):
                id_type = _attr(ident, "identificationTypeCode")
                number = _attr(ident, "number")
                country = _attr(ident, "countryIso2Code")
                if number:
                    identifiers.append({
                        "type": id_type,
                        "value": number,
                        "country": country,
                    })

            # Addresses
            addresses: list[str] = []
            for addr in entity.findall("address"):
                parts = []
                street = _attr(addr, "street")
                city = _attr(addr, "city")
                country = _attr(addr, "countryIso2Code")
                if street:
                    parts.append(street)
                if city:
                    parts.append(city)
                if country:
                    parts.append(country)
                if parts:
                    addresses.append(", ".join(parts))

            # Programme from regulation
            programs: list[str] = []
            for reg in entity.findall("regulation"):
                prog = _attr(reg, "programme")
                if prog and prog not in programs:
                    programs.append(prog)

            # Designation / function from nameAlias
            designation = None
            for alias in entity.findall("nameAlias"):
                func = _attr(alias, "function")
                if func:
                    designation = func
                    break

            entries.append(ListEntry(
                id=f"eu-{eu_ref}" if eu_ref else f"eu-{names[0]}",
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
                designation=designation,
                programs=programs,
                source_url=SOURCE_URL,
            ))
        except Exception:
            logger.exception("Failed to parse EU sanctionEntity")
            continue

    logger.info("Parsed %d individuals from EU FSF", len(entries))
    return entries
