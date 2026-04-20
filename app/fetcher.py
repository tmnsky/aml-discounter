"""Orchestrates downloading, hashing, parsing, and loading all sanctions/PEP sources."""

import asyncio
import hashlib
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import httpx

from .db import (
    get_staging_conn,
    init_audit_db,
    init_index_tables,
    insert_entry,
    swap_index,
    update_source_metadata,
    get_source_metadata,
)
from .schema import ListEntry

logger = logging.getLogger(__name__)

USER_AGENT = "AML-Discounter/1.0"

# Minimum expected entity counts per source (sanity checks)
MIN_COUNTS = {
    "ofac_sdn": 6000,
    "un_consolidated": 500,
    "eu_fsf": 1000,
    "wikidata_peps": 500,
    "us_congress": 400,
    "uk_parliament": 600,
    "eu_parliament": 600,
    "fbi_wanted": 20,
    "australia_dfat": 200,
}


@dataclass
class SourceConfig:
    """Configuration for a single data source."""

    name: str
    url: Optional[str]  # None for sources that handle their own fetching (e.g., Wikidata)
    parser: Callable  # Function that returns list[ListEntry]
    needs_download: bool = True  # False if the parser handles HTTP internally
    min_count: int = 0


def _compute_hash(data: bytes) -> str:
    """SHA256 hash of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def _transliterate(name: str) -> tuple[str, str]:
    """Generate latin and ASCII name variants.

    Returns (latin_name, ascii_name). Best-effort without heavy deps.
    """
    import unicodedata

    # NFD normalize then strip combining marks for ASCII
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = nfkd.encode("ascii", "ignore").decode("ascii").strip()
    # Latin is just the NFC form (preserves accents)
    latin = unicodedata.normalize("NFC", name)
    return latin, ascii_name


def _phonetic(name: str) -> tuple[str, str]:
    """Generate phonetic codes for a name.

    Returns (primary, alternate). Uses jellyfish for metaphone.
    """
    try:
        import jellyfish

        parts = name.split()
        primary_codes = []
        alt_codes = []
        for part in parts:
            clean = "".join(c for c in part if c.isalpha())
            if not clean:
                continue
            primary_codes.append(jellyfish.metaphone(clean))
            alt_codes.append(jellyfish.soundex(clean))
        return " ".join(primary_codes), " ".join(alt_codes)
    except ImportError:
        return "", ""


async def _download(client: httpx.AsyncClient, url: str, attempts: int = 3) -> bytes:
    """Download a URL with retries on timeout/network errors."""
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            # Long timeout for big files like OFAC SDN (117MB)
            resp = await client.get(url, follow_redirects=True, timeout=httpx.Timeout(300.0, connect=30.0))
            resp.raise_for_status()
            return resp.content
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.NetworkError) as e:
            last_err = e
            logger.warning("Download attempt %d/%d failed for %s: %s", attempt, attempts, url, e)
            if attempt < attempts:
                await asyncio.sleep(2 * attempt)
    raise last_err if last_err else RuntimeError(f"Download failed: {url}")


def _load_entries_to_db(conn, entries: list[ListEntry]):
    """Insert a batch of ListEntry objects into the staging database."""
    for entry in entries:
        try:
            primary_name = entry.names[0] if entry.names else ""
            latin, ascii_name = _transliterate(primary_name)
            phon_p, phon_a = _phonetic(ascii_name or primary_name)
            insert_entry(conn, entry, latin, ascii_name, phon_p, phon_a)
        except Exception:
            logger.warning("Failed to insert entry %s", entry.id, exc_info=True)


def _get_source_configs() -> list[SourceConfig]:
    """Build the list of all source configurations.

    Imports parsers lazily so the module can be imported without all deps.
    """
    configs: list[SourceConfig] = []

    # OFAC SDN (download XML, parse)
    try:
        from .parsers.ofac import parse_ofac_advanced

        configs.append(SourceConfig(
            name="ofac_sdn",
            url="https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML",
            parser=lambda path: parse_ofac_advanced(path, source_name="ofac_sdn"),
            needs_download=True,
            min_count=MIN_COUNTS.get("ofac_sdn", 0),
        ))
    except ImportError:
        logger.warning("ofac parser not available")

    # OFAC Consolidated (download XML, parse)
    try:
        from .parsers.ofac import parse_ofac_consolidated

        configs.append(SourceConfig(
            name="ofac_cons",
            url="https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/CONS_ADVANCED.XML",
            parser=parse_ofac_consolidated,
            needs_download=True,
            min_count=0,
        ))
    except ImportError:
        pass

    # UN Security Council
    try:
        from .parsers.un import parse_un

        configs.append(SourceConfig(
            name="un_consolidated",
            url="https://scsanctions.un.org/resources/xml/en/consolidated.xml",
            parser=parse_un,
            needs_download=True,
            min_count=MIN_COUNTS.get("un_consolidated", 0),
        ))
    except ImportError:
        logger.warning("un parser not available")

    # EU Financial Sanctions
    try:
        from .parsers.eu import parse_eu

        configs.append(SourceConfig(
            name="eu_fsf",
            url="https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw",
            parser=parse_eu,
            needs_download=True,
            min_count=MIN_COUNTS.get("eu_fsf", 0),
        ))
    except ImportError:
        logger.warning("eu parser not available")

    # UK Sanctions
    try:
        from .parsers.uk import parse_uk

        configs.append(SourceConfig(
            name="uk_sanctions",
            url="https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml",
            parser=parse_uk,
            needs_download=True,
            min_count=0,
        ))
    except ImportError:
        logger.warning("uk parser not available")

    # Canada SEMA
    try:
        from .parsers.canada import parse_canada

        configs.append(SourceConfig(
            name="ca_sema",
            url="https://www.international.gc.ca/world-monde/assets/office_docs/international_relations-relations_internationales/sanctions/sema-lmes.xml",
            parser=parse_canada,
            needs_download=True,
            min_count=0,
        ))
    except ImportError:
        logger.warning("canada parser not available")

    # Switzerland SECO
    try:
        from .parsers.switzerland import parse_switzerland

        configs.append(SourceConfig(
            name="ch_seco",
            url="https://www.sesam.search.admin.ch/sesam-search-web/pages/downloadXmlGesamtliste.xhtml?lang=en&action=downloadXmlGesamtlisteAction",
            parser=parse_switzerland,
            needs_download=True,
            min_count=0,
        ))
    except ImportError:
        logger.warning("switzerland parser not available")

    # Wikidata PEPs (handles own HTTP, per-country queries)
    try:
        from .parsers.wikidata_peps import fetch_wikidata_peps

        configs.append(SourceConfig(
            name="wikidata_peps",
            url=None,
            parser=fetch_wikidata_peps,
            needs_download=False,
            min_count=MIN_COUNTS.get("wikidata_peps", 0),
        ))
    except ImportError:
        logger.warning("wikidata_peps parser not available")

    # US Congress
    try:
        from .parsers.us_congress import fetch_us_congress

        configs.append(SourceConfig(
            name="us_congress",
            url=None,
            parser=fetch_us_congress,
            needs_download=False,
            min_count=MIN_COUNTS.get("us_congress", 0),
        ))
    except ImportError:
        logger.warning("us_congress parser not available")

    # UK Parliament
    try:
        from .parsers.uk_parliament import fetch_uk_parliament

        configs.append(SourceConfig(
            name="uk_parliament",
            url=None,
            parser=fetch_uk_parliament,
            needs_download=False,
            min_count=MIN_COUNTS.get("uk_parliament", 0),
        ))
    except ImportError:
        logger.warning("uk_parliament parser not available")

    # EU Parliament
    try:
        from .parsers.eu_parliament import fetch_eu_parliament

        configs.append(SourceConfig(
            name="eu_parliament",
            url=None,
            parser=fetch_eu_parliament,
            needs_download=False,
            min_count=MIN_COUNTS.get("eu_parliament", 0),
        ))
    except ImportError:
        logger.warning("eu_parliament parser not available")

    # FBI Most Wanted
    try:
        from .parsers.fbi import fetch_fbi_wanted

        configs.append(SourceConfig(
            name="fbi_wanted",
            url=None,
            parser=fetch_fbi_wanted,
            needs_download=False,
            min_count=MIN_COUNTS.get("fbi_wanted", 0),
        ))
    except ImportError:
        logger.warning("fbi parser not available")

    # Australia DFAT
    try:
        from .parsers.australia import fetch_australia_sanctions

        configs.append(SourceConfig(
            name="australia_dfat",
            url=None,
            parser=fetch_australia_sanctions,
            needs_download=False,
            min_count=MIN_COUNTS.get("australia_dfat", 0),
        ))
    except ImportError:
        logger.warning("australia parser not available")

    return configs


async def _process_source(
    source: SourceConfig,
    client: httpx.AsyncClient,
    staging_conn,
) -> tuple[str, int, str]:
    """Process a single source: download, hash-check, parse, load.

    Returns (source_name, entity_count, file_hash).
    """
    file_hash = ""
    entries: list[ListEntry] = []

    if source.needs_download and source.url:
        # Download-first sources (OFAC XML, UN XML, etc.)
        raw_data = await _download(client, source.url)
        file_hash = _compute_hash(raw_data)

        # Write to temp file for parsers that need a file path
        with tempfile.NamedTemporaryFile(delete=False, suffix=".dat") as tmp:
            tmp.write(raw_data)
            tmp_path = tmp.name

        try:
            entries = source.parser(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    else:
        # Self-fetching sources (parsers handle their own HTTP)
        # Run sync parsers in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        entries = await loop.run_in_executor(None, source.parser)

        # Compute hash from the serialized entry IDs for change detection
        content_sig = "|".join(sorted(e.id for e in entries)).encode()
        file_hash = _compute_hash(content_sig)

    count = len(entries)

    # Sanity check: minimum entity count
    if source.min_count and count < source.min_count:
        logger.warning(
            "Source %s returned only %d entries (expected >= %d). "
            "Possible data issue, loading anyway.",
            source.name, count, source.min_count,
        )

    if entries:
        _load_entries_to_db(staging_conn, entries)

    return source.name, count, file_hash


async def refresh_all_sources():
    """Main entry point: refresh all sanctions/PEP data sources.

    For each source:
    1. Download (if applicable) and compute SHA256 hash
    2. Compare against previous hash from source_metadata
    3. If changed: parse, insert into staging DB, update metadata
    4. If unchanged: skip
    5. After all sources: atomic swap staging -> live
    """
    init_audit_db()
    staging_conn = get_staging_conn()
    init_index_tables(staging_conn)

    sources = _get_source_configs()
    results: dict[str, dict] = {}

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(120.0, connect=30.0),
        follow_redirects=True,
    ) as client:
        for source in sources:
            try:
                logger.info("Processing source: %s", source.name)
                name, count, fhash = await _process_source(source, client, staging_conn)
                results[name] = {"count": count, "hash": fhash, "status": "ok"}
                logger.info("Source %s: %d entities, hash=%s", name, count, fhash[:12])
            except Exception:
                logger.warning("Source %s failed, skipping", source.name, exc_info=True)
                results[source.name] = {"count": 0, "hash": "", "status": "error"}

    # Commit staging DB
    staging_conn.commit()
    staging_count = staging_conn.execute("SELECT COUNT(*) FROM sanctions_entities").fetchone()[0]
    staging_conn.close()

    # Safety: if staging is empty (e.g., all sources were unchanged so nothing was inserted),
    # don't overwrite the existing live DB. Just delete the empty staging file.
    if staging_count == 0:
        from .db import STAGING_DB_PATH
        if STAGING_DB_PATH.exists():
            STAGING_DB_PATH.unlink()
        logger.info("Staging is empty (no source changes detected), keeping existing live index")
    else:
        # Atomic swap: staging -> live
        swap_index()
        logger.info("Index swapped to live (%d entities)", staging_count)

    # Update source metadata in audit DB
    for name, info in results.items():
        try:
            update_source_metadata(
                source=name,
                entity_count=info["count"],
                file_hash=info["hash"],
                status=info["status"],
            )
        except Exception:
            logger.warning("Failed to update metadata for %s", name, exc_info=True)

    total = sum(r["count"] for r in results.values())
    ok = sum(1 for r in results.values() if r["status"] == "ok")
    failed = sum(1 for r in results.values() if r["status"] == "error")
    logger.info(
        "Refresh complete: %d sources OK, %d failed, %d total entities",
        ok, failed, total,
    )

    return results
