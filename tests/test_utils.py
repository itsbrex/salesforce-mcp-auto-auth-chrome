"""Tests for the shared cookie-reader helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from salesforce_mcp_auto_auth_chrome.utils import best_host_match, query_sqlite_cookies


def test_query_sqlite_cookies_reads_rows(tmp_path: Path) -> None:
    db = tmp_path / "Cookies"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cookies (host TEXT, name TEXT, value TEXT)")
    con.execute("INSERT INTO cookies VALUES ('acme.my.salesforce.com', 'sid', 'S')")
    con.commit()
    con.close()

    rows = query_sqlite_cookies(db, "SELECT host, name, value FROM cookies")

    assert rows == [("acme.my.salesforce.com", "sid", "S")]


def test_query_sqlite_cookies_reads_uncheckpointed_wal_rows(tmp_path: Path) -> None:
    # A cookie written to a WAL-mode store lives only in the ``-wal`` sidecar
    # until a checkpoint. Copying the main DB alone would miss it, so the helper
    # must copy the sidecars too. Reproduce that exact state: a committed row
    # held in the WAL by a still-open writer with auto-checkpoint disabled.
    db = tmp_path / "Cookies"
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE cookies (host TEXT, name TEXT, value TEXT)")
    host = "acme.my.salesforce.com"
    writer.execute("INSERT INTO cookies VALUES (?, 'sid', 'OLD')", (host,))
    writer.commit()
    # This second write stays in the -wal file (no checkpoint, connection stays
    # open holding the main DB, wal, and shm files just like a live browser).
    writer.execute("INSERT INTO cookies VALUES (?, 'sid', 'FRESH')", (host,))
    writer.commit()
    try:
        assert (db.with_name("Cookies-wal")).exists()  # sidecar really is present

        rows = query_sqlite_cookies(
            db, "SELECT value FROM cookies WHERE name = 'sid' ORDER BY value"
        )
    finally:
        writer.close()

    # The freshly written sid, which existed only in the WAL, must be visible.
    assert rows == [("FRESH",), ("OLD",)]


def test_query_sqlite_cookies_returns_none_for_missing_db(tmp_path: Path) -> None:
    assert query_sqlite_cookies(tmp_path / "nope", "SELECT 1") is None


def test_best_host_match_prefers_longest_eligible_suffix() -> None:
    rows = [
        ("salesforce.com", "A"),
        ("acme.my.salesforce.com", "B"),
        ("my.salesforce.com", "C"),
    ]
    match = best_host_match(
        rows, "acme.my.salesforce.com", host_of=lambda r: r[0]
    )
    assert match == ("acme.my.salesforce.com", "B")
