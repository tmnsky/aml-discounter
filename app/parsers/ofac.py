"""OFAC SDN Advanced XML parser.

Parses the OFAC SDN Advanced XML format (used for both the SDN list and the
Consolidated Non-SDN list) into ListEntry objects. Uses full-doc load with
lxml for random-access joins across the four XML sections.
"""

import logging
from typing import Optional

from lxml import etree

from ..schema import ListEntry

log = logging.getLogger(__name__)

# Part-type IDs from OFAC reference data
_PART_LAST = "1520"
_PART_FIRST = "1521"
_PART_MIDDLE = "1522"
_PART_PATRONYMIC = "91708"
_PART_ORDER = {_PART_LAST: 0, _PART_FIRST: 1, _PART_MIDDLE: 2, _PART_PATRONYMIC: 3}

# Feature-type IDs
_FEAT_DOB = "8"
_FEAT_POB = "9"
_FEAT_NATIONALITY = "10"
_FEAT_CITIZENSHIP = "11"
_FEAT_ADDRESS = "25"
_FEAT_GENDER = "224"
_GENDER_MAP = {"91526": "male", "91527": "female"}

# Latin script
_SCRIPT_LATIN = "215"

_SOURCE_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ADVANCED_XML"


def _strip_ns(tree: etree._ElementTree) -> None:
    """Remove all namespaces from the document in-place.

    OFAC changed their namespace URI in May 2024, so we strip entirely
    to avoid brittle namespace-prefixed XPath.
    """
    root = tree.getroot()
    for elem in root.iter():
        elem.tag = etree.QName(elem).localname
        for attr_key in list(elem.attrib):
            local = etree.QName(attr_key).localname
            if local != attr_key:
                elem.attrib[local] = elem.attrib.pop(attr_key)
    etree.cleanup_namespaces(root)


def _build_ref_index(ref_sets: etree._Element) -> dict[str, dict[str, etree._Element]]:
    """Index ReferenceValueSets as {TypeName: {ID: element}}."""
    idx: dict[str, dict[str, etree._Element]] = {}
    for ref_set in ref_sets:
        type_name = ref_set.tag  # e.g. "PartySubTypeValues"
        entries: dict[str, etree._Element] = {}
        for val in ref_set:
            vid = val.get("ID")
            if vid:
                entries[vid] = val
        if entries:
            idx[type_name] = entries
    return idx


def _ref_text(ref: dict[str, dict[str, etree._Element]], type_name: str, vid: str) -> str:
    """Look up reference value text."""
    vals = ref.get(type_name, {})
    el = vals.get(vid)
    return el.text.strip() if el is not None and el.text else ""


def _index_by(section: Optional[etree._Element], tag: str, key_attr: str) -> dict[str, list[etree._Element]]:
    """Index child elements by an attribute, allowing multiple per key."""
    idx: dict[str, list[etree._Element]] = {}
    if section is None:
        return idx
    for child in section.findall(tag):
        k = child.get(key_attr) or child.findtext(key_attr, "")
        if k:
            idx.setdefault(k, []).append(child)
    return idx


def _index_single(section: Optional[etree._Element], tag: str, key_attr: str) -> dict[str, etree._Element]:
    """Index child elements by attribute, single per key."""
    idx: dict[str, etree._Element] = {}
    if section is None:
        return idx
    for child in section.findall(tag):
        k = child.get(key_attr) or child.findtext(key_attr, "")
        if k:
            idx[k] = child
    return idx


def _parse_date(date_period: etree._Element) -> tuple[str, bool]:
    """Parse a DatePeriod element into (date_string, is_approximate).

    Handles:
      - Exact dates: Start/From/Year+Month+Day
      - Year-only: Jan-1 to Dec-31 of same year -> "YYYY"
      - Partial dates: year+month only -> "YYYY-MM"
    """
    approximate = False

    # Try Start or From sub-elements
    for wrapper_tag in ("Start", "From"):
        wrapper = date_period.find(wrapper_tag)
        if wrapper is not None:
            year = wrapper.findtext("Year")
            month = wrapper.findtext("Month")
            day = wrapper.findtext("Day")
            if year:
                return _format_date_parts(year, month, day, date_period)

    # Direct Year/Month/Day children
    year = date_period.findtext("Year")
    month = date_period.findtext("Month")
    day = date_period.findtext("Day")
    if year:
        return _format_date_parts(year, month, day, date_period)

    return ("", False)


def _format_date_parts(
    year: str,
    month: Optional[str],
    day: Optional[str],
    date_period: etree._Element,
) -> tuple[str, bool]:
    """Format year/month/day parts, detecting year-only ranges."""
    y = year.strip()
    m = month.strip() if month else None
    d = day.strip() if day else None

    # Detect year-only: check if this is a range spanning Jan-1 to Dec-31
    end = date_period.find("End") or date_period.find("To")
    if end is not None and m and d:
        end_y = (end.findtext("Year") or "").strip()
        end_m = (end.findtext("Month") or "").strip()
        end_d = (end.findtext("Day") or "").strip()
        if end_y == y and end_m == "12" and end_d == "31" and m == "1" and d == "1":
            return (y, True)

    if not m:
        return (y, True)
    if not d:
        return (f"{y}-{int(m):02d}", True)
    return (f"{y}-{int(m):02d}-{int(d):02d}", False)


def _assemble_names(
    identity: etree._Element,
) -> tuple[list[str], list[str]]:
    """Extract all names from an Identity element.

    Returns (names, alias_qualities) where index 0 is the primary name.
    """
    names: list[str] = []
    qualities: list[str] = []

    # Build part-type map from NamePartGroups
    part_type_map: dict[str, str] = {}  # NamePartGroupID -> NamePartTypeID
    for npg in identity.iter("NamePartGroup"):
        gpid = npg.get("ID", "")
        npt_el = npg.find("NamePartType")
        if npt_el is not None:
            part_type_map[gpid] = npt_el.get("NamePartTypeID", "")

    # Collect aliases: primary first, then rest
    aliases = list(identity.iter("Alias"))
    primary_aliases = [a for a in aliases if a.get("Primary") == "true"]
    other_aliases = [a for a in aliases if a.get("Primary") != "true"]

    for alias in primary_aliases + other_aliases:
        is_primary = alias.get("Primary") == "true"
        is_weak = alias.get("LowQuality") == "true"
        quality = "weak" if is_weak else ("strong" if is_primary else "strong")

        for doc_name in alias.findall("DocumentedName"):
            parts: list[tuple[int, str]] = []  # (sort_order, value)
            for dnp in doc_name.findall("DocumentedNamePart"):
                npv = dnp.find("NamePartValue")
                if npv is None or not npv.text:
                    continue
                gpid = npv.get("NamePartGroupID", "")
                type_id = part_type_map.get(gpid, "")
                order = _PART_ORDER.get(type_id, 99)

                # Only include Latin script or unspecified
                script = npv.get("ScriptID", _SCRIPT_LATIN)
                if script != _SCRIPT_LATIN:
                    continue

                parts.append((order, npv.text.strip()))

            if parts:
                # Sort by part order: first, middle, patronymic, last
                parts.sort(key=lambda p: (p[0] if p[0] != 0 else 99,))
                # Assemble: first middle last
                ordered = []
                last_parts = [v for o, v in parts if o == 0]
                first_parts = [v for o, v in parts if o == 1]
                middle_parts = [v for o, v in parts if o == 2]
                patron_parts = [v for o, v in parts if o == 3]
                other_parts = [v for o, v in parts if o == 99]

                ordered.extend(first_parts)
                ordered.extend(middle_parts)
                ordered.extend(patron_parts)
                ordered.extend(last_parts)
                ordered.extend(other_parts)

                full_name = " ".join(ordered)
                if full_name and full_name not in names:
                    names.append(full_name)
                    qualities.append(quality)

    return names, qualities


def _extract_features(
    profile: etree._Element,
    ref: dict[str, dict[str, etree._Element]],
    locations_idx: dict[str, etree._Element],
) -> dict:
    """Extract DOB, POB, nationality, citizenship, address, gender from Features."""
    result: dict = {
        "dob": [],
        "dob_approximate": False,
        "pob": [],
        "nationality": [],
        "addresses": [],
        "gender": None,
    }

    for feature in profile.iter("Feature"):
        ftype = feature.get("FeatureTypeID", "")

        if ftype == _FEAT_DOB:
            dp = feature.find(".//DatePeriod")
            if dp is not None:
                date_str, approx = _parse_date(dp)
                if date_str:
                    result["dob"].append(date_str)
                    if approx:
                        result["dob_approximate"] = True

        elif ftype == _FEAT_POB:
            loc_el = feature.find(".//VersionLocation")
            if loc_el is not None:
                loc_id = loc_el.get("LocationID", "")
                place = _resolve_location(loc_id, locations_idx, ref)
                if place:
                    result["pob"].append(place)

        elif ftype in (_FEAT_NATIONALITY, _FEAT_CITIZENSHIP):
            vc = feature.find("FeatureVersion/VersionDetail")
            if vc is not None:
                ref_id = vc.get("DetailReferenceID", "")
                country = _ref_text(ref, "AreaCodeValues", ref_id)
                if not country:
                    country = _ref_text(ref, "CountryValues", ref_id)
                if country and country not in result["nationality"]:
                    result["nationality"].append(country)

        elif ftype == _FEAT_ADDRESS:
            loc_el = feature.find(".//VersionLocation")
            if loc_el is not None:
                loc_id = loc_el.get("LocationID", "")
                addr = _resolve_location(loc_id, locations_idx, ref)
                if addr:
                    result["addresses"].append(addr)

        elif ftype == _FEAT_GENDER:
            vd = feature.find("FeatureVersion/VersionDetail")
            if vd is not None:
                ref_id = vd.get("DetailReferenceID", "")
                result["gender"] = _GENDER_MAP.get(ref_id)

    return result


def _resolve_location(
    loc_id: str,
    locations_idx: dict[str, etree._Element],
    ref: dict[str, dict[str, etree._Element]],
) -> str:
    """Resolve a LocationID to a human-readable address string."""
    loc = locations_idx.get(loc_id)
    if loc is None:
        return ""

    parts = []
    for part in loc.findall("LocationPart"):
        for val in part.findall("LocationPartValue"):
            v = val.findtext("Value", "").strip()
            if v:
                parts.append(v)

    # Also grab country from LocationCountry
    country_el = loc.find("LocationCountry")
    if country_el is not None:
        cid = country_el.get("CountryID", "")
        country = _ref_text(ref, "CountryValues", cid)
        if country and country not in parts:
            parts.append(country)

    return ", ".join(parts)


def _extract_id_docs(
    identity_id: str,
    id_reg_idx: dict[str, list[etree._Element]],
) -> list[dict]:
    """Extract identity documents for an identity."""
    docs = []
    for doc_el in id_reg_idx.get(identity_id, []):
        doc_type_el = doc_el.find("IDRegistrationDocType")
        id_number = (doc_el.findtext("IDRegistrationNo") or "").strip()
        if not id_number:
            continue

        doc_type = doc_type_el.text.strip() if doc_type_el is not None and doc_type_el.text else "Unknown"
        country_el = doc_el.find("IssuingCountry")
        country = ""
        if country_el is not None:
            country = (country_el.get("CountryID") or country_el.text or "").strip()

        docs.append({
            "type": doc_type,
            "value": id_number,
            "country": country,
        })

    return docs


def _extract_programs(
    profile_id: str,
    sanctions_idx: dict[str, list[etree._Element]],
    ref: dict[str, dict[str, etree._Element]],
) -> tuple[list[str], Optional[str]]:
    """Extract programs and listing date from SanctionsEntries."""
    programs: list[str] = []
    listed_on: Optional[str] = None

    for entry in sanctions_idx.get(profile_id, []):
        # Programs from SanctionsMeasure
        for measure in entry.findall("SanctionsMeasure"):
            st_id = measure.findtext("SanctionsTypeID", "").strip()
            st_text = _ref_text(ref, "SanctionsTypeValues", st_id)
            if st_text.lower() == "program" or not st_text:
                comment = (measure.findtext("Comment") or "").strip()
                if comment and comment not in programs:
                    programs.append(comment)

        # Listing date from EntryEvent/Date (with Year/Month/Day child elements)
        for event in entry.findall("EntryEvent"):
            date_el = event.find("Date")
            if date_el is not None and not listed_on:
                year = (date_el.findtext("Year") or "").strip()
                month = (date_el.findtext("Month") or "").strip()
                day = (date_el.findtext("Day") or "").strip()
                if year:
                    # Build ISO date (YYYY-MM-DD or YYYY-MM or YYYY)
                    parts = [year.zfill(4)]
                    if month:
                        parts.append(month.zfill(2))
                        if day:
                            parts.append(day.zfill(2))
                    listed_on = "-".join(parts)

    return programs, listed_on


def parse_ofac_advanced(xml_path: str, source_name: str = "ofac_sdn") -> list[ListEntry]:
    """Parse OFAC SDN Advanced XML into ListEntry objects.

    Args:
        xml_path: Path to the downloaded Advanced XML file.
        source_name: Source identifier ("ofac_sdn" or "ofac_cons").

    Returns:
        List of ListEntry for each individual in the file.
    """
    list_name_map = {
        "ofac_sdn": "OFAC SDN List",
        "ofac_cons": "OFAC Consolidated Non-SDN List",
    }
    list_name = list_name_map.get(source_name, f"OFAC {source_name}")

    log.info("Parsing %s from %s", source_name, xml_path)

    # Full-doc load (random access required for cross-section joins)
    parser = etree.XMLParser(huge_tree=True)
    tree = etree.parse(xml_path, parser=parser)
    _strip_ns(tree)
    root = tree.getroot()

    # Pre-index reference value sets
    ref_section = root.find("ReferenceValueSets")
    ref = _build_ref_index(ref_section) if ref_section is not None else {}

    # Determine which PartySubType IDs map (via PartyTypeID) to "Individual"
    # PartyTypeValues has the actual type names; PartySubTypeValues references them by PartyTypeID
    individual_party_type_ids: set[str] = set()
    for vid, el in ref.get("PartyTypeValues", {}).items():
        if el.text and el.text.strip().lower() == "individual":
            individual_party_type_ids.add(vid)

    individual_ids: set[str] = set()
    for vid, el in ref.get("PartySubTypeValues", {}).items():
        party_type_id = el.get("PartyTypeID", "")
        if party_type_id in individual_party_type_ids:
            individual_ids.add(vid)

    # Index Locations by ID
    locations_section = root.find("Locations")
    locations_idx = _index_single(locations_section, "Location", "ID")

    # Index IDRegDocuments by IdentityID (NOT ProfileID)
    id_reg_section = root.find("IDRegDocuments")
    id_reg_idx = _index_by(id_reg_section, "IDRegDocument", "IdentityID")

    # Index SanctionsEntries by ProfileID
    sanctions_section = root.find("SanctionsEntries")
    sanctions_idx = _index_by(sanctions_section, "SanctionsEntry", "ProfileID")

    # Walk DistinctParties
    entries: list[ListEntry] = []
    parties_section = root.find("DistinctParties")
    if parties_section is None:
        log.warning("No DistinctParties section found in %s", xml_path)
        return entries

    for party in parties_section.findall("DistinctParty"):
        # Filter to individuals only
        profile = party.find("Profile")
        if profile is None:
            continue

        party_sub = profile.get("PartySubTypeID", "")
        if party_sub not in individual_ids:
            continue

        profile_id = profile.get("ID", "")
        fixed_ref = party.get("FixedRef", profile_id)

        # Get Identity for names and docs
        identity = profile.find("Identity")
        if identity is None:
            continue

        identity_id = identity.get("ID", "")

        # Names
        names, qualities = _assemble_names(identity)
        if not names:
            continue

        # Features (DOB, POB, nationality, gender, address)
        feats = _extract_features(profile, ref, locations_idx)

        # Identity documents
        id_docs = _extract_id_docs(identity_id, id_reg_idx)

        # Programs and listing date
        programs, listed_on = _extract_programs(profile_id, sanctions_idx, ref)

        entry = ListEntry(
            id=f"ofac-{fixed_ref}",
            source=source_name,
            list_name=list_name,
            names=names,
            alias_quality=qualities,
            dob=feats["dob"],
            dob_approximate=feats["dob_approximate"],
            pob=feats["pob"],
            nationality=feats["nationality"],
            gender=feats["gender"],
            identifiers=id_docs,
            addresses=feats["addresses"],
            programs=programs,
            listed_on=listed_on,
            source_url=_SOURCE_URL,
        )
        entries.append(entry)

    log.info("Parsed %d individuals from %s", len(entries), source_name)
    return entries


def parse_ofac_consolidated(xml_path: str) -> list[ListEntry]:
    """Parse OFAC Consolidated Non-SDN XML (same format, different source tag)."""
    return parse_ofac_advanced(xml_path, source_name="ofac_cons")
