"""Parse Australian DFAT Consolidated Sanctions List from XLSX."""

import io
import logging
import re
from collections import defaultdict

import httpx
import openpyxl

from ..schema import ListEntry

logger = logging.getLogger(__name__)

DFAT_URL = "https://www.dfat.gov.au/sites/default/files/Australian_Sanctions_Consolidated_List.xlsx"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _parse_messy_date(raw: str) -> str:
    """Best-effort parse of DFAT's messy date formats.

    Handles: "1 Jan 1970", "01/01/1970", "1970", "between 1963 and 1968",
    "circa 1960", "approximately 1975", corrupt/partial values.
    Returns ISO date string, year-only, range description, or empty string.
    """
    if not raw:
        return ""
    raw = str(raw).strip()

    # "between X and Y" ranges
    between = re.match(r"between\s+(\d{4})\s+and\s+(\d{4})", raw, re.IGNORECASE)
    if between:
        return f"{between.group(1)}-{between.group(2)}"

    # "circa 1960", "approximately 1975"
    circa = re.match(r"(?:circa|approx(?:imately)?)\s+(\d{4})", raw, re.IGNORECASE)
    if circa:
        return f"~{circa.group(1)}"

    # ISO-ish: "1970-01-01" or "1970-1-1"
    iso = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if iso:
        return f"{iso.group(1)}-{int(iso.group(2)):02d}-{int(iso.group(3)):02d}"

    # DD/MM/YYYY or D/M/YYYY
    slash = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if slash:
        return f"{slash.group(3)}-{int(slash.group(2)):02d}-{int(slash.group(1)):02d}"

    # "1 Jan 1970", "01 January 1970"
    months = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
        "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }
    text_date = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", raw)
    if text_date:
        month_str = text_date.group(2)[:3].lower()
        if month_str in months:
            return f"{text_date.group(3)}-{months[month_str]}-{int(text_date.group(1)):02d}"

    # Year only
    year_only = re.match(r"^(\d{4})$", raw)
    if year_only:
        return year_only.group(1)

    # Last resort: extract any 4-digit year
    year_extract = re.search(r"\b(\d{4})\b", raw)
    if year_extract:
        y = int(year_extract.group(1))
        if 1900 <= y <= 2030:
            return str(y)

    return ""


def _extract_reference_base(ref: str) -> str:
    """Extract the numeric base from a reference like '101a' -> '101'."""
    match = re.match(r"^(\d+)", str(ref).strip())
    return match.group(1) if match else str(ref).strip()


def fetch_australia_sanctions() -> list[ListEntry]:
    """Parse the Australian DFAT Consolidated Sanctions XLSX.

    Groups rows by reference number base (101, 101a, 101b = one entity).
    Returns empty list with warning if geo-blocked (common outside AU/allied countries).
    """
    try:
        resp = httpx.get(
            DFAT_URL,
            headers={"User-Agent": BROWSER_UA},
            timeout=120,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        logger.warning("Australia DFAT sanctions geo-blocked or unreachable: %s", e)
        return []
    except Exception:
        logger.warning("Failed to download Australia DFAT sanctions XLSX", exc_info=True)
        return []

    wb = openpyxl.load_workbook(io.BytesIO(resp.content), read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if len(rows) < 2:
        logger.warning("Australia DFAT XLSX has no data rows")
        return []

    # Find header row (first row with "Reference" or "Name")
    header = [str(c or "").strip().lower() for c in rows[0]]
    col_map = {}
    for i, h in enumerate(header):
        if "reference" in h:
            col_map["ref"] = i
        elif h in ("name", "name of individual or entity"):
            col_map["name"] = i
        elif "type" in h and "entity" not in h:
            col_map["type"] = i
        elif "date of birth" in h or "dob" in h:
            col_map["dob"] = i
        elif "place of birth" in h or "pob" in h:
            col_map["pob"] = i
        elif "nationality" in h or "citizenship" in h:
            col_map["nationality"] = i
        elif "designation" in h or "title" in h:
            col_map["designation"] = i
        elif "listing" in h and "date" in h:
            col_map["listed_on"] = i
        elif "additional" in h or "other" in h:
            col_map["additional"] = i
        elif "alias" in h:
            col_map["alias"] = i

    if "name" not in col_map:
        logger.warning("Australia DFAT XLSX: could not find 'name' column in header: %s", header)
        return []

    # Group rows by reference number base
    groups: dict[str, list[tuple]] = defaultdict(list)
    for row in rows[1:]:
        if not row or all(c is None for c in row):
            continue
        ref = str(row[col_map.get("ref", 0)] or "").strip()
        if not ref:
            continue
        base = _extract_reference_base(ref)
        groups[base].append(row)

    entries: list[ListEntry] = []
    for base_ref, group_rows in groups.items():
        try:
            all_names: list[str] = []
            dobs: list[str] = []
            pobs: list[str] = []
            nationalities: list[str] = []
            designation = ""
            listed_on = ""

            for row in group_rows:
                name = str(row[col_map["name"]] or "").strip() if "name" in col_map else ""
                if name and name not in all_names:
                    all_names.append(name)

                if "alias" in col_map:
                    alias = str(row[col_map["alias"]] or "").strip()
                    if alias and alias not in all_names:
                        all_names.append(alias)

                if "dob" in col_map:
                    d = _parse_messy_date(str(row[col_map["dob"]] or ""))
                    if d and d not in dobs:
                        dobs.append(d)

                if "pob" in col_map:
                    p = str(row[col_map["pob"]] or "").strip()
                    if p and p not in pobs:
                        pobs.append(p)

                if "nationality" in col_map:
                    n = str(row[col_map["nationality"]] or "").strip()
                    if n and n not in nationalities:
                        nationalities.append(n)

                if "designation" in col_map and not designation:
                    designation = str(row[col_map["designation"]] or "").strip()

                if "listed_on" in col_map and not listed_on:
                    listed_on = _parse_messy_date(str(row[col_map["listed_on"]] or ""))

            if not all_names:
                continue

            # Determine if DOBs are approximate
            dob_approx = any("~" in d or "-" in d and len(d) == 9 for d in dobs)

            entries.append(ListEntry(
                id=f"au-{base_ref}",
                source="australia_dfat",
                list_name="Australian DFAT Consolidated Sanctions List",
                names=all_names,
                dob=dobs,
                dob_approximate=dob_approx,
                pob=pobs,
                nationality=nationalities,
                designation=designation or None,
                listed_on=listed_on or None,
                source_url=DFAT_URL,
                raw={"reference": base_ref, "row_count": len(group_rows)},
            ))
        except Exception:
            logger.warning("Skipping bad Australia DFAT group ref=%s", base_ref, exc_info=True)

    logger.info("Australia DFAT sanctions entries parsed: %d", len(entries))
    return entries
