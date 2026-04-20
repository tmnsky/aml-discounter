"""Parse Canada SEMA (Special Economic Measures Act) Sanctions XML."""

import logging
import re
from datetime import datetime
from lxml import etree

from ..schema import ListEntry

logger = logging.getLogger(__name__)

SOURCE = "canada_sema"
LIST_NAME = "Canada SEMA Consolidated List"
SOURCE_URL = "https://www.international.gc.ca/world-monde/international_relations-relations_internationales/sanctions/consolidated-consolide.aspx"
FETCH_URL = "https://www.international.gc.ca/world-monde/assets/office_docs/international_relations-relations_internationales/sanctions/sema-lmes.xml"

# Language prefix pattern for extracting transliterations: "Russian: Олег..."
LANG_PREFIX_RE = re.compile(r"^([A-Za-z]+):\s*(.+)$")

DATE_FORMATS = ["%Y-%m-%d", "%d-%m-%Y", "%b-%y", "%Y"]


def _text(el: etree._Element, tag: str) -> str:
    """Get text of a direct child element, or empty string."""
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _parse_country(raw: str) -> str:
    """Extract English part from bilingual country names like 'Russia / Russie'."""
    if " / " in raw:
        return raw.split(" / ")[0].strip()
    return raw.strip()


def _parse_date(raw: str) -> str | None:
    """Try multiple date formats. Returns ISO string or year, or None on failure."""
    if not raw:
        return None

    # Skip obviously corrupt values (e.g. "31801")
    stripped = raw.strip()
    if stripped.isdigit() and len(stripped) == 5:
        return None

    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(stripped, fmt)
            if fmt == "%Y":
                return stripped
            if fmt == "%b-%y":
                return str(dt.year)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    logger.debug("Unparseable Canada date: %r", raw)
    return None


def _extract_aliases(name: str) -> list[str]:
    """Extract language-prefixed aliases from a name field.

    e.g. 'Russian: Олег Иванович' -> ['Олег Иванович']
    """
    aliases = []
    match = LANG_PREFIX_RE.match(name)
    if match:
        aliases.append(match.group(2))
    return aliases


def parse_canada(xml_path: str) -> list[ListEntry]:
    """Parse Canada SEMA Sanctions XML into ListEntry objects."""
    tree = etree.parse(xml_path)
    root = tree.getroot()

    entries: list[ListEntry] = []
    for record in root.iter("record"):
        try:
            given = _text(record, "GivenName")
            last = _text(record, "LastName")
            dob_raw = _text(record, "DateOfBirth")

            # Determine if this is an individual (has name parts or DOB)
            if not given and not last and not dob_raw:
                continue

            # Build primary name
            name_parts = []
            if given:
                name_parts.append(given)
            if last:
                name_parts.append(last)
            primary_name = " ".join(name_parts)
            if not primary_name:
                continue

            names = [primary_name]
            alias_quality = ["strong"]

            # Extract aliases from language-prefixed name fields
            for field_name in ("GivenName", "LastName"):
                val = _text(record, field_name)
                for alias in _extract_aliases(val):
                    if alias and alias.lower() != primary_name.lower():
                        names.append(alias)
                        alias_quality.append("weak")

            # Aliases field
            aliases_raw = _text(record, "Aliases")
            if aliases_raw:
                for alias in re.split(r"[;,]", aliases_raw):
                    alias = alias.strip()
                    if alias and alias.lower() != primary_name.lower():
                        # Check for language prefix
                        lang_aliases = _extract_aliases(alias)
                        if lang_aliases:
                            for la in lang_aliases:
                                if la.lower() != primary_name.lower():
                                    names.append(la)
                                    alias_quality.append("weak")
                            # Also keep the unprefixed alias text
                            names.append(alias)
                            alias_quality.append("weak")
                        else:
                            names.append(alias)
                            alias_quality.append("unknown")

            # Date of birth
            dobs: list[str] = []
            dob_approx = False
            if dob_raw:
                parsed = _parse_date(dob_raw)
                if parsed:
                    dobs.append(parsed)

            # Country
            country_raw = _text(record, "Country")
            nationality: list[str] = []
            if country_raw:
                nationality.append(_parse_country(country_raw))

            # Schedule / program
            programs: list[str] = []
            schedule = _text(record, "Schedule")
            if schedule:
                programs.append(schedule)
            item = _text(record, "Item")
            if item:
                programs.append(f"Item {item}")

            entries.append(ListEntry(
                id=f"ca-{last}-{given}".replace(" ", "_") if last else f"ca-{primary_name}",
                source=SOURCE,
                list_name=LIST_NAME,
                names=names,
                alias_quality=alias_quality,
                dob=dobs,
                dob_approximate=dob_approx,
                nationality=nationality,
                programs=programs,
                source_url=SOURCE_URL,
            ))
        except Exception:
            logger.exception("Failed to parse Canada SEMA record")
            continue

    logger.info("Parsed %d individuals from Canada SEMA", len(entries))
    return entries
