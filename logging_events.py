"""
logging_events.py — Immutable audit log for all medication safety events.

Design rules:
  - Every action writes a row. No silent failures.
  - Confirmed events can ONLY be corrected via CAREGIVER_CORRECTION (never deleted).
  - Raw transcript always stored for post-incident review.
  - All timestamps in UTC; timezone conversion is caller's responsibility.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import os as _os
DB_PATH_DEFAULT = _os.getenv("DB_PATH", "med_safety.db")

# ─── Valid event states (mirrors schema.sql comment) ─────────────────────────
VALID_STATES = {
    "MED_CONFIRMED",
    "MED_UNCERTAIN",
    "MED_SKIPPED",
    "MED_DUPLICATE_BLOCKED",
    "MED_AMBIGUOUS",
    "MED_LOW_CONFIDENCE",
    "DRUG_INTERACTION_ALERT",
    "CAREGIVER_ESCALATION",
    "EMERGENCY_ESCALATION",
    "CAREGIVER_CORRECTION",
}


# ─── Core log writer ─────────────────────────────────────────────────────────

def log_event(
    state_status:     str,
    raw_transcript:   str,
    medication_id:    Optional[int]   = None,
    confidence_score: Optional[float] = None,
    input_mode:       str             = "text",
    resolved_by:      Optional[str]   = None,
    notes:            Optional[str]   = None,
    dose_window_date: Optional[str]   = None,    # defaults to today UTC
    db_path:          str             = DB_PATH_DEFAULT,
) -> int:
    """
    Write one audit row. Returns the new row ID.

    Raises ValueError for unknown state_status — prevents silent typos.
    """
    if state_status not in VALID_STATES:
        raise ValueError(
            f"Unknown state_status {state_status!r}. Valid: {sorted(VALID_STATES)}"
        )

    if dose_window_date is None:
        dose_window_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO ingestion_logs
            (medication_id, dose_window_date, input_mode, state_status,
             raw_transcript, confidence_score, resolved_by, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            medication_id,
            dose_window_date,
            input_mode,
            state_status,
            raw_transcript,
            confidence_score,
            resolved_by,
            notes,
        ),
    )
    row_id = cur.lastrowid
    con.commit()
    con.close()
    return row_id


# ─── Convenience wrappers ─────────────────────────────────────────────────────

def log_confirmed(
    medication_id:    int,
    raw_transcript:   str,
    confidence_score: Optional[float] = None,
    input_mode:       str             = "text",
    db_path:          str             = DB_PATH_DEFAULT,
) -> int:
    return log_event(
        state_status="MED_CONFIRMED",
        raw_transcript=raw_transcript,
        medication_id=medication_id,
        confidence_score=confidence_score,
        input_mode=input_mode,
        resolved_by="user",
        db_path=db_path,
    )


def log_uncertain(
    raw_transcript:   str,
    medication_id:    Optional[int]   = None,
    confidence_score: Optional[float] = None,
    notes:            Optional[str]   = None,
    db_path:          str             = DB_PATH_DEFAULT,
) -> int:
    """
    User unsure if dose was taken. Never treat uncertain as confirmed.
    medication_id may be None if identity also unknown.
    """
    return log_event(
        state_status="MED_UNCERTAIN",
        raw_transcript=raw_transcript,
        medication_id=medication_id,
        confidence_score=confidence_score,
        notes=notes,
        db_path=db_path,
    )


def log_duplicate_blocked(
    medication_id:  int,
    raw_transcript: str,
    existing_log_id: int,
    db_path:        str = DB_PATH_DEFAULT,
) -> int:
    return log_event(
        state_status="MED_DUPLICATE_BLOCKED",
        raw_transcript=raw_transcript,
        medication_id=medication_id,
        notes=f"blocked; prior log id={existing_log_id}",
        db_path=db_path,
    )


def log_ambiguous(
    raw_transcript:   str,
    candidate_ids:    list[int],
    confidence_score: Optional[float] = None,
    db_path:          str             = DB_PATH_DEFAULT,
) -> int:
    return log_event(
        state_status="MED_AMBIGUOUS",
        raw_transcript=raw_transcript,
        confidence_score=confidence_score,
        notes=f"candidates={json.dumps(candidate_ids)}",
        db_path=db_path,
    )


def log_low_confidence(
    raw_transcript:   str,
    confidence_score: float,
    db_path:          str = DB_PATH_DEFAULT,
) -> int:
    return log_event(
        state_status="MED_LOW_CONFIDENCE",
        raw_transcript=raw_transcript,
        confidence_score=confidence_score,
        notes=f"score {confidence_score:.2f} below threshold",
        db_path=db_path,
    )


def log_escalation(
    raw_transcript: str,
    reason:         str,
    medication_id:  Optional[int] = None,
    emergency:      bool          = False,
    db_path:        str           = DB_PATH_DEFAULT,
) -> int:
    status = "EMERGENCY_ESCALATION" if emergency else "DRUG_INTERACTION_ALERT"
    return log_event(
        state_status=status,
        raw_transcript=raw_transcript,
        medication_id=medication_id,
        notes=reason,
        db_path=db_path,
    )


def log_caregiver_correction(
    original_log_id: int,
    medication_id:   int,
    raw_transcript:  str,
    correction_note: str,
    db_path:         str = DB_PATH_DEFAULT,
) -> int:
    """
    Caregiver corrects existing log. Original row is NEVER deleted.
    New correction row references the original via notes.
    """
    return log_event(
        state_status="CAREGIVER_CORRECTION",
        raw_transcript=raw_transcript,
        medication_id=medication_id,
        resolved_by="caregiver",
        notes=f"corrects log id={original_log_id}. {correction_note}",
        db_path=db_path,
    )


# ─── Query helpers ────────────────────────────────────────────────────────────

def get_confirmed_today(
    medication_id:    int,
    dose_window_date: Optional[str] = None,
    db_path:          str           = DB_PATH_DEFAULT,
) -> Optional[dict]:
    """
    Return the first confirmed log for this med today, or None.
    Used by duplicate_guard.py.
    """
    if dose_window_date is None:
        dose_window_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(
        """
        SELECT * FROM ingestion_logs
        WHERE medication_id = ?
          AND dose_window_date = ?
          AND state_status = 'MED_CONFIRMED'
        ORDER BY logged_at ASC
        LIMIT 1
        """,
        (medication_id, dose_window_date),
    )
    row = cur.fetchone()
    con.close()
    return dict(row) if row else None


def get_recent_logs(
    limit:   int = 20,
    db_path: str = DB_PATH_DEFAULT,
) -> list[dict]:
    """Return most recent log rows for CLI review / caregiver dashboard."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(
        """
        SELECT l.*, m.clinical_name, m.nickname
        FROM ingestion_logs l
        LEFT JOIN medications m ON l.medication_id = m.id
        ORDER BY l.logged_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


# ─── Quick smoke test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    row_id = log_confirmed(
        medication_id=1,
        raw_transcript="I took my heart pill",
        confidence_score=0.95,
    )
    print(f"Logged MED_CONFIRMED → row id {row_id}")

    row_id2 = log_uncertain(
        raw_transcript="I think I took it, not sure",
        notes="user self-reported uncertainty",
    )
    print(f"Logged MED_UNCERTAIN → row id {row_id2}")

    recent = get_recent_logs(limit=5)
    for r in recent:
        print(r)
