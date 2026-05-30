"""
db.py — Shared SQLite connection helper.

All modules that need DB access should use get_connection() from here
to keep path resolution consistent.
"""

import os
import sqlite3

DB_PATH = os.getenv("DB_PATH", "med_safety.db")


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """
    Return a sqlite3 connection with row_factory and foreign key enforcement.
    Caller is responsible for closing the connection.
    """
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def ensure_schema(db_path: str | None = None) -> None:
    """Apply schema.sql to db_path if tables don't exist yet."""
    path = db_path or DB_PATH
    schema_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    con = sqlite3.connect(path)
    with open(schema_file) as f:
        con.executescript(f.read())
    con.commit()
    con.close()
