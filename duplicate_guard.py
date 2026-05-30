"""
duplicate_guard.py — Deterministic duplicate-dose prevention.

Rules:
  - A confirmed dose within the active window BLOCKS another dose.
  - Window = scheduled_time ± DOSE_WINDOW_HOURS (from patient_profile).
  - An UNCERTAIN event also blocks — uncertain ≠ safe to re-dose.
  - Guard is called BEFORE any MED_CONFIRMED is written.
  - LLM cannot override this check.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

from logging_events import (
    log_duplicate_blocked,
    log_uncertain,
    get_confirmed_today,
    DB_PATH_DEFAULT,
)

DEFAULT_WINDOW_HOURS = 6


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_profile_value(key: str, db_path: str) -> Optional[str]:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT value FROM patient_profile WHERE key = ?", (key,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None


def _window_hours(db_path: str) -> int:
    val = _get_profile_value("dose_window_hours", db_path)
    try:
        return int(val) if val else DEFAULT_WINDOW_HOURS
    except (ValueError, TypeError):
        return DEFAULT_WINDOW_HOURS


def _dose_window_date(db_path: str) -> str:
    """
    Return YYYY-MM-DD in patient's local timezone.
    Falls back to UTC if timezone not set or invalid.
    """
    tz_name = _get_profile_value("schedule_timezone", db_path)
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(tz_name) if tz_name else timezone.utc
        return datetime.now(tz).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _get_recent_log(
    medication_id:    int,
    dose_window_date: str,
    statuses:         tuple[str, ...],
    db_path:          str,
) -> Optional[dict]:
    """
    Return most recent log row matching medication_id + date + any of statuses.
    Covers both today and yesterday (for cross-midnight schedules).
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Check today AND yesterday to handle cross-midnight window
    yesterday = (
        datetime.fromisoformat(dose_window_date) - timedelta(days=1)
    ).strftime("%Y-%m-%d")

    placeholders = ",".join("?" * len(statuses))
    cur.execute(
        f"""
        SELECT * FROM ingestion_logs
        WHERE medication_id = ?
          AND dose_window_date IN (?, ?)
          AND state_status IN ({placeholders})
        ORDER BY logged_at DESC
        LIMIT 1
        """,
        (medication_id, dose_window_date, yesterday, *statuses),
    )
    row = cur.fetchone()
    con.close()
    return dict(row) if row else None


def _within_window(logged_at_str: str, scheduled_time: str, window_hours: int) -> bool:
    """
    True if logged_at falls within window_hours of scheduled_time today or yesterday.
    Both sides: dose logged early and dose attempted late both caught.
    """
    try:
        logged_at = datetime.fromisoformat(logged_at_str).replace(tzinfo=timezone.utc)
    except ValueError:
        return True  # fail-safe: assume within window if we can't parse

    # Build scheduled datetime anchors (today + yesterday in UTC)
    now = datetime.now(timezone.utc)
    h, m = int(scheduled_time[:2]), int(scheduled_time[3:5])
    anchors = [
        now.replace(hour=h, minute=m, second=0, microsecond=0),
        (now - timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0),
    ]

    half = timedelta(hours=window_hours)
    return any(abs(logged_at - anchor) <= half for anchor in anchors)


# ─── GuardResult ─────────────────────────────────────────────────────────────

class GuardResult:
    ALLOWED  = "ALLOWED"   # safe to proceed to MED_CONFIRMED
    BLOCKED  = "BLOCKED"   # duplicate detected; do not confirm

    def __init__(
        self,
        outcome:       str,
        reason:        str          = "",
        existing_log:  Optional[dict] = None,
        block_log_id:  Optional[int]  = None,
    ):
        self.outcome      = outcome
        self.reason       = reason
        self.existing_log = existing_log
        self.block_log_id = block_log_id

    @property
    def is_allowed(self) -> bool:
        return self.outcome == self.ALLOWED


# ─── Core guard ──────────────────────────────────────────────────────────────

def check_duplicate(
    medication_id:  int,
    raw_transcript: str,
    db_path:        str = DB_PATH_DEFAULT,
) -> GuardResult:
    """
    Main entry point. Call this BEFORE writing MED_CONFIRMED.

    Returns BLOCKED if:
      - A MED_CONFIRMED exists within the dose window.
      - A MED_UNCERTAIN exists within the dose window
        (uncertain ≠ permission to redose).

    Returns ALLOWED only when no prior confirmed or uncertain log exists
    within the active window.
    """
    window_hours      = _window_hours(db_path)
    dose_window_date  = _dose_window_date(db_path)

    # ── Check 1: confirmed dose ───────────────────────────────────────────────
    confirmed = _get_recent_log(
        medication_id, dose_window_date, ("MED_CONFIRMED",), db_path
    )
    if confirmed:
        med = _get_med(medication_id, db_path)
        scheduled = (med or {}).get("scheduled_time", "00:00")
        if _within_window(confirmed["logged_at"], scheduled, window_hours):
            block_id = log_duplicate_blocked(
                medication_id=medication_id,
                raw_transcript=raw_transcript,
                existing_log_id=confirmed["id"],
                db_path=db_path,
            )
            return GuardResult(
                outcome=GuardResult.BLOCKED,
                reason=(
                    f"Already confirmed at {confirmed['logged_at'][:16]} UTC. "
                    f"Next dose not due for {window_hours}h."
                ),
                existing_log=confirmed,
                block_log_id=block_id,
            )

    # ── Check 2: uncertain dose (also blocks re-dose) ─────────────────────────
    uncertain = _get_recent_log(
        medication_id, dose_window_date, ("MED_UNCERTAIN",), db_path
    )
    if uncertain:
        med = _get_med(medication_id, db_path)
        scheduled = (med or {}).get("scheduled_time", "00:00")
        if _within_window(uncertain["logged_at"], scheduled, window_hours):
            block_id = log_duplicate_blocked(
                medication_id=medication_id,
                raw_transcript=raw_transcript,
                existing_log_id=uncertain["id"],
                db_path=db_path,
            )
            return GuardResult(
                outcome=GuardResult.BLOCKED,
                reason=(
                    "An uncertain dose was logged at "
                    f"{uncertain['logged_at'][:16]} UTC. "
                    "Do not take another dose until a caregiver confirms."
                ),
                existing_log=uncertain,
                block_log_id=block_id,
            )

    return GuardResult(outcome=GuardResult.ALLOWED)


def _get_med(medication_id: int, db_path: str) -> Optional[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM medications WHERE id = ?", (medication_id,))
    row = cur.fetchone()
    con.close()
    return dict(row) if row else None
