"""
caregiver_override.py — Explicit two-step caregiver correction with audit trail.

Rules:
  - Original log rows are NEVER deleted or modified.
  - Override requires explicit verbal/text confirmation ("yes, correct the record").
  - Every correction writes a CAREGIVER_CORRECTION row referencing the original.
  - Caregiver identity stored in correction note (from patient_profile).
  - Pending override sessions expire after OVERRIDE_TIMEOUT_MINUTES.
  - A caregiver cannot retroactively convert MED_UNCERTAIN → MED_CONFIRMED
    without also supplying the correct medication_id.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

from logging_events import log_caregiver_correction, log_event, DB_PATH_DEFAULT

OVERRIDE_TIMEOUT_MINUTES = 5

# Phrases accepted as explicit confirmation
CONFIRM_PHRASES = {
    "yes, correct the record",
    "yes correct the record",
    "confirm",
    "yes",
    "correct",
    "yes, update",
    "yes update",
}


# ─── Pending override store (in-memory + DB) ─────────────────────────────────
# Keyed by caregiver session token (simple timestamp-based string).
# In a real app this would be a proper session store; for demo, in-memory dict
# is sufficient since caregiver interacts in a single session.

_pending_overrides: dict[str, dict] = {}


# ─── OverrideResult ───────────────────────────────────────────────────────────

class OverrideResult:
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    CONFIRMED             = "CONFIRMED"
    CANCELLED             = "CANCELLED"
    EXPIRED               = "EXPIRED"
    INVALID               = "INVALID"

    def __init__(
        self,
        outcome:        str,
        prompt:         Optional[str] = None,
        correction_id:  Optional[int] = None,
        original_log:   Optional[dict] = None,
    ):
        self.outcome       = outcome
        self.prompt        = prompt
        self.correction_id = correction_id
        self.original_log  = original_log

    @property
    def is_done(self) -> bool:
        return self.outcome in (self.CONFIRMED, self.CANCELLED, self.EXPIRED, self.INVALID)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_log_row(log_id: int, db_path: str) -> Optional[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(
        """
        SELECT l.*, m.clinical_name, m.nickname
        FROM ingestion_logs l
        LEFT JOIN medications m ON l.medication_id = m.id
        WHERE l.id = ?
        """,
        (log_id,),
    )
    row = cur.fetchone()
    con.close()
    return dict(row) if row else None


def _get_profile_value(key: str, db_path: str) -> Optional[str]:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT value FROM patient_profile WHERE key = ?", (key,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None


def _make_override_token() -> str:
    return "ovr_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


# ─── Core API ─────────────────────────────────────────────────────────────────

def request_override(
    original_log_id:     int,
    correction_note:     str,
    new_medication_id:   Optional[int] = None,
    db_path:             str           = DB_PATH_DEFAULT,
) -> OverrideResult:
    """
    Step 1: Caregiver requests a correction.

    Retrieves the original log, shows caregiver what will change,
    returns a confirmation prompt and a token for step 2.
    """
    original = _get_log_row(original_log_id, db_path)
    if not original:
        return OverrideResult(
            outcome=OverrideResult.INVALID,
            prompt=f"No log record found with id={original_log_id}.",
        )

    # Build human-readable description of what will change
    med_name = original.get("clinical_name") or "unknown medication"
    logged_at = original["logged_at"][:16]
    current_status = original["state_status"]

    if new_medication_id:
        # Changing the medication identity on the record
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT clinical_name FROM medications WHERE id = ?", (new_medication_id,))
        row = cur.fetchone()
        con.close()
        new_name = dict(row)["clinical_name"] if row else f"id={new_medication_id}"
        change_desc = f"Change medication from '{med_name}' to '{new_name}'"
    else:
        change_desc = f"Correct record for '{med_name}'"

    prompt = (
        f"To update the record logged at {logged_at} (status: {current_status}):\n"
        f"  {change_desc}\n"
        f"  Reason: {correction_note}\n\n"
        "To confirm this change, say: **yes, correct the record**\n"
        "This action will be saved with your name and timestamp."
    )

    token = _make_override_token()
    _pending_overrides[token] = {
        "original_log_id":   original_log_id,
        "new_medication_id": new_medication_id,
        "correction_note":   correction_note,
        "db_path":           db_path,
        "created_at":        datetime.now(timezone.utc).isoformat(),
        "original":          original,
    }

    return OverrideResult(
        outcome=OverrideResult.AWAITING_CONFIRMATION,
        prompt=prompt,
        original_log=original,
    ), token  # type: ignore[return-value]
    # Note: returns (OverrideResult, token) tuple — caller unpacks


def confirm_override(
    token:        str,
    user_response: str,
    db_path:      str = DB_PATH_DEFAULT,
) -> OverrideResult:
    """
    Step 2: Caregiver confirms the override.

    Accepts the token from request_override and the caregiver's response.
    Returns CONFIRMED only on exact confirmation phrase.
    """
    pending = _pending_overrides.get(token)
    if not pending:
        return OverrideResult(
            outcome=OverrideResult.INVALID,
            prompt="No pending override found. Please start the correction again.",
        )

    # Check timeout
    created = datetime.fromisoformat(pending["created_at"]).replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - created) > timedelta(minutes=OVERRIDE_TIMEOUT_MINUTES):
        del _pending_overrides[token]
        return OverrideResult(
            outcome=OverrideResult.EXPIRED,
            prompt="Override request timed out. Please start again.",
        )

    normalised = user_response.strip().lower().rstrip(".")

    # Explicit cancel
    if normalised in {"no", "cancel", "stop", "never mind", "nevermind"}:
        del _pending_overrides[token]
        return OverrideResult(
            outcome=OverrideResult.CANCELLED,
            prompt="Correction cancelled. The original record has not been changed.",
        )

    # Must match a confirmation phrase — exact, no interpretation
    if normalised not in CONFIRM_PHRASES:
        return OverrideResult(
            outcome=OverrideResult.AWAITING_CONFIRMATION,
            prompt=(
                f"I didn't recognise that as a confirmation.\n"
                "To confirm, say exactly: **yes, correct the record**\n"
                "Or say 'cancel' to stop."
            ),
        )

    # ── Write correction ──────────────────────────────────────────────────────
    original_log_id   = pending["original_log_id"]
    new_medication_id = pending["new_medication_id"]
    correction_note   = pending["correction_note"]
    original          = pending["original"]
    db_path           = pending["db_path"]  # use stored path, not caller's

    caregiver_name = _get_profile_value("caregiver_name", db_path) or "caregiver"
    effective_med_id = new_medication_id if new_medication_id is not None \
                       else original.get("medication_id")

    correction_id = log_caregiver_correction(
        original_log_id=original_log_id,
        medication_id=effective_med_id,
        raw_transcript=f"[caregiver override] {correction_note}",
        correction_note=f"Corrected by {caregiver_name}. {correction_note}",
        db_path=db_path,
    )

    del _pending_overrides[token]

    correction_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return OverrideResult(
        outcome=OverrideResult.CONFIRMED,
        prompt=(
            f"Done. Correction recorded at {correction_time}.\n"
            f"Audit note saved: corrected by {caregiver_name}."
        ),
        correction_id=correction_id,
        original_log=original,
    )


def get_corrections_for_log(
    original_log_id: int,
    db_path:         str = DB_PATH_DEFAULT,
) -> list[dict]:
    """Return all CAREGIVER_CORRECTION rows that reference a given original log."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(
        """
        SELECT * FROM ingestion_logs
        WHERE state_status = 'CAREGIVER_CORRECTION'
          AND notes LIKE ?
        ORDER BY logged_at ASC
        """,
        (f"%corrects log id={original_log_id}%",),
    )
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows
