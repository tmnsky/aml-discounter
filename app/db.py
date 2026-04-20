"""SQLite database: entity storage, FTS5 index, audit log."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Optional

from .schema import ListEntry

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "aml_discounter.db"
INDEX_DB_PATH = DATA_DIR / "sanctions_index.db"
STAGING_DB_PATH = DATA_DIR / "sanctions_index_staging.db"


def get_audit_conn() -> sqlite3.Connection:
    """Get connection to the audit/screening database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_index_conn() -> sqlite3.Connection:
    """Get read-only connection to the sanctions index."""
    if not INDEX_DB_PATH.exists():
        raise FileNotFoundError(
            "Sanctions index not found. Run 'aml-screen --refresh' to fetch data."
        )
    conn = sqlite3.connect(f"file:{INDEX_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_staging_conn() -> sqlite3.Connection:
    """Get connection to the staging index (for building new data)."""
    STAGING_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STAGING_DB_PATH.exists():
        STAGING_DB_PATH.unlink()
    conn = sqlite3.connect(str(STAGING_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")  # 64MB cache for bulk load
    return conn


def init_audit_db():
    """Create audit tables if they don't exist."""
    conn = get_audit_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS screenings (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            user_input TEXT NOT NULL,
            source_versions TEXT NOT NULL,
            raw_candidates INTEGER,
            unique_persons INTEGER,
            auto_cleared INTEGER,
            auto_flagged INTEGER,
            sent_to_llm INTEGER,
            llm_cleared INTEGER,
            llm_flagged INTEGER,
            llm_escalated INTEGER,
            result TEXT NOT NULL,
            matches TEXT NOT NULL,
            llm_calls TEXT NOT NULL,
            report_json TEXT NOT NULL,
            processing_ms INTEGER,
            screened_by TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS source_metadata (
            source TEXT PRIMARY KEY,
            last_fetched TEXT,
            entity_count INTEGER,
            file_hash TEXT,
            status TEXT DEFAULT 'ok'
        );
    """
    )
    conn.close()


def init_index_tables(conn: sqlite3.Connection):
    """Create sanctions entity and FTS5 tables in the given connection."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sanctions_entities (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            list_name TEXT NOT NULL,
            name TEXT NOT NULL,
            name_latin TEXT,
            name_ascii TEXT,
            aliases TEXT,
            phonetic_primary TEXT,
            phonetic_alt TEXT,
            dob TEXT,
            dob_approximate INTEGER DEFAULT 0,
            pob TEXT,
            nationality TEXT,
            gender TEXT,
            identifiers TEXT,
            addresses TEXT,
            designation TEXT,
            listing_reason TEXT,
            listed_on TEXT,
            programs TEXT,
            source_url TEXT,
            raw_json TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS sanctions_fts USING fts5(
            name,
            name_latin,
            name_ascii,
            aliases,
            phonetic_primary,
            phonetic_alt,
            tokenize = 'unicode61 remove_diacritics 2',
            prefix = '2 3',
            content = 'sanctions_entities',
            content_rowid = 'rowid'
        );

        CREATE TRIGGER IF NOT EXISTS sanctions_ai AFTER INSERT ON sanctions_entities BEGIN
            INSERT INTO sanctions_fts(rowid, name, name_latin, name_ascii, aliases, phonetic_primary, phonetic_alt)
            VALUES (new.rowid, new.name, new.name_latin, new.name_ascii, new.aliases, new.phonetic_primary, new.phonetic_alt);
        END;

        CREATE INDEX IF NOT EXISTS idx_sanctions_source ON sanctions_entities(source);
        CREATE INDEX IF NOT EXISTS idx_sanctions_id ON sanctions_entities(id);
    """
    )


def insert_entry(conn: sqlite3.Connection, entry: ListEntry, latin: str, ascii_name: str, phonetic_p: str, phonetic_a: str):
    """Insert a ListEntry into the sanctions index."""
    conn.execute(
        """INSERT OR REPLACE INTO sanctions_entities
        (id, source, list_name, name, name_latin, name_ascii, aliases,
         phonetic_primary, phonetic_alt, dob, dob_approximate, pob,
         nationality, gender, identifiers, addresses, designation,
         listing_reason, listed_on, programs, source_url, raw_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            entry.id,
            entry.source,
            entry.list_name,
            entry.names[0] if entry.names else "",
            latin,
            ascii_name,
            "|".join(entry.names[1:]) if len(entry.names) > 1 else "",
            phonetic_p,
            phonetic_a,
            "|".join(entry.dob),
            1 if entry.dob_approximate else 0,
            "|".join(entry.pob),
            "|".join(entry.nationality),
            entry.gender,
            json.dumps(entry.identifiers),
            "|".join(entry.addresses),
            entry.designation,
            entry.listing_reason,
            entry.listed_on,
            "|".join(entry.programs),
            entry.source_url,
            json.dumps(entry.raw),
        ),
    )


def swap_index():
    """Atomically swap staging index to live."""
    if STAGING_DB_PATH.exists():
        os.rename(str(STAGING_DB_PATH), str(INDEX_DB_PATH))


def get_source_metadata(source: str) -> Optional[dict]:
    """Get metadata for a source from audit DB."""
    conn = get_audit_conn()
    row = conn.execute(
        "SELECT * FROM source_metadata WHERE source = ?", (source,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_source_metadata(source: str, entity_count: int, file_hash: str, status: str = "ok"):
    """Update source metadata after a fetch."""
    from datetime import datetime

    conn = get_audit_conn()
    conn.execute(
        """INSERT OR REPLACE INTO source_metadata (source, last_fetched, entity_count, file_hash, status)
        VALUES (?, ?, ?, ?, ?)""",
        (source, datetime.utcnow().isoformat(), entity_count, file_hash, status),
    )
    conn.commit()
    conn.close()


def save_screening(result: dict):
    """Save a screening result to the audit log."""
    conn = get_audit_conn()
    conn.execute(
        """INSERT INTO screenings
        (id, created_at, user_input, source_versions, raw_candidates, unique_persons,
         auto_cleared, auto_flagged, sent_to_llm, llm_cleared, llm_flagged, llm_escalated,
         result, matches, llm_calls, report_json, processing_ms, screened_by)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            result["id"],
            result["timestamp"],
            json.dumps(result["user_input"]),
            json.dumps(result["source_versions"]),
            result["raw_candidates"],
            result["unique_persons"],
            result["auto_cleared"],
            result["auto_flagged"],
            result["sent_to_llm"],
            result["llm_cleared"],
            result["llm_flagged"],
            result["llm_escalated"],
            result["result"],
            json.dumps(result["matches"]),
            json.dumps(result["llm_calls"]),
            json.dumps(result),
            result["processing_ms"],
            result.get("screened_by", ""),
        ),
    )
    conn.commit()
    conn.close()


def file_hash(path: str) -> str:
    """SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
