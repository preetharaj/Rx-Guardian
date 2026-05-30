"""
ambiguity_handler.py — Multi-turn clarification for ambiguous pill matches.

Rules:
  - NEVER auto-select when multiple candidates exist.
  - NEVER log MED_CONFIRMED until user resolves to exactly one med.
  - Session expires after MAX_PENDING_MINUTES; expired = MED_UNCERTAIN.
  - If user cannot clarify → MED_UNCERTAIN, notify caregiver.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

from lookup import lookup_medication, LookupStatus, candidates_summary, resolve_by_id
from logging_events import log_ambiguous, log_uncertain, log_confirmed, DB_PATH_DEFAULT

MAX_PENDING_MINUTES = 10  # session older than this → expired


# ─── Data class ──────────────────────────────────────────────────────────────

class AmbiguitySession:
    """In-flight clarification session, backed by disambiguation_sessions table."""

    def __init__(self, row: dict):
        self.id              = row["id"]
        self.session_key     = row["session_key"]
        self.candidate_ids   = json.loads(row["candidate_ids"])
        self.raw_transcript  = row["raw_transcript"]
        self.created_at      = row["created_at"]
        self.status          = row["status"]

    @property
    def is_expired(self) -> bool:
        try:
            created = datetime.fromisoformat(self.created_at).replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        return (datetime.now(timezone.utc) - created) > timedelta(minutes=MAX_PENDING_MINUTES)

    def candidate_count(self) -> int:
        return len(self.candidate_ids)


# ─── Session management ───────────────────────────────────────────────────────

def _make_session_key() -> str:
    return "ambig_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def create_session(
    candidate_ids:  list[int],
    raw_transcript: str,
    db_path:        str = DB_PATH_DEFAULT,
) -> AmbiguitySession:
    """Open a new pending clarification session. Log MED_AMBIGUOUS."""
    key = _make_session_key()
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO disambiguation_sessions
            (session_key, candidate_ids, raw_transcript, status)
        VALUES (?, ?, ?, 'pending')
        """,
        (key, json.dumps(candidate_ids), raw_transcript),
    )
    con.commit()
    row_id = cur.lastrowid
    cur.execute("SELECT * FROM disambiguation_sessions WHERE id = ?", (row_id,))
    session = AmbiguitySession(dict(cur.fetchone()))
    con.close()

    log_ambiguous(raw_transcript, candidate_ids, db_path=db_path)
    return session


def get_pending_session(
    session_key: str,
    db_path:     str = DB_PATH_DEFAULT,
) -> Optional[AmbiguitySession]:
    """Return pending session or None if resolved/expired/missing."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(
        "SELECT * FROM disambiguation_sessions WHERE session_key = ? AND status = 'pending'",
        (session_key,),
    )
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    session = AmbiguitySession(dict(row))
    if session.is_expired:
        _mark_session(session_key, "expired", db_path=db_path)
        return None
    return session


def _mark_session(
    session_key:     str,
    status:          str,
    resolved_med_id: Optional[int] = None,
    db_path:         str           = DB_PATH_DEFAULT,
) -> None:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute(
        """
        UPDATE disambiguation_sessions
        SET status = ?, resolved_at = datetime('now'), resolved_med_id = ?
        WHERE session_key = ?
        """,
        (status, resolved_med_id, session_key),
    )
    con.commit()
    con.close()


# ─── Core public API ──────────────────────────────────────────────────────────

class ClarificationResult:
    """Returned by handle_ambiguous_input and resolve_clarification."""
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    RESOLVED            = "RESOLVED"
    UNRESOLVABLE        = "UNRESOLVABLE"   # user gave up or session expired

    def __init__(
        self,
        outcome:        str,
        session_key:    Optional[str]  = None,
        medication:     Optional[dict] = None,
        prompt:         Optional[str]  = None,
        log_id:         Optional[int]  = None,
    ):
        self.outcome     = outcome
        self.session_key = session_key
        self.medication  = medication
        self.prompt      = prompt
        self.log_id      = log_id


def handle_ambiguous_input(
    raw_transcript:   str,
    candidates:       list[dict],
    confidence_score: Optional[float] = None,
    db_path:          str             = DB_PATH_DEFAULT,
) -> ClarificationResult:
    """
    Called when lookup returns AMBIGUOUS. Opens session, returns prompt.
    Caller must surface prompt to user and wait for clarification reply.
    """
    candidate_ids = [m["id"] for m in candidates]
    session = create_session(candidate_ids, raw_transcript, db_path=db_path)

    summary = candidates_summary(candidates)
    prompt = (
        f'I found more than one pill that could match "{raw_transcript}".\n\n'
        f"{summary}\n\n"
        "Which one did you take? You can say the number, or describe the bottle."
    )

    return ClarificationResult(
        outcome=ClarificationResult.NEEDS_CLARIFICATION,
        session_key=session.session_key,
        prompt=prompt,
    )


def resolve_clarification(
    session_key:   str,
    user_response: str,
    db_path:       str = DB_PATH_DEFAULT,
) -> ClarificationResult:
    """
    Called with user's clarification reply. Tries to match to one candidate.

    Returns:
      RESOLVED        → medication confirmed; caller logs MED_CONFIRMED
      NEEDS_CLARIFICATION → still ambiguous; returns new prompt
      UNRESOLVABLE    → cannot resolve; logs MED_UNCERTAIN
    """
    session = get_pending_session(session_key, db_path=db_path)

    # Expired or already resolved
    if session is None:
        log_id = log_uncertain(
            raw_transcript=user_response,
            notes="ambiguity session expired or not found",
            db_path=db_path,
        )
        return ClarificationResult(
            outcome=ClarificationResult.UNRESOLVABLE,
            log_id=log_id,
            prompt="Sorry, that clarification took too long. I've marked this dose as uncertain. Please ask your caregiver to check.",
        )

    # Try numeric selection ("1", "2", etc.)
    stripped = user_response.strip()
    if stripped.isdigit():
        choice = int(stripped) - 1  # convert to 0-based index
        if 0 <= choice < len(session.candidate_ids):
            med_id = session.candidate_ids[choice]
            return _confirm_resolution(session, med_id, user_response, db_path)
        # Out of range
        return _bad_clarification(session, user_response,
                                  "That number isn't one of the options.", db_path)

    # Try re-running lookup on the clarification text
    result = lookup_medication(user_response, db_path=db_path)

    if result.status == LookupStatus.MATCH and result.match:
        med_id = result.match["id"]
        # Verify the resolved med is actually a candidate in this session
        if med_id in session.candidate_ids:
            return _confirm_resolution(session, med_id, user_response, db_path)
        # Match is valid but not a candidate — different med mentioned
        return _bad_clarification(session, user_response,
                                  "That doesn't match any of the options I listed.", db_path)

    if result.status == LookupStatus.AMBIGUOUS:
        # Still ambiguous after clarification — one more prompt
        candidates = result.candidates
        # Narrow to intersection of original candidates + new matches
        narrowed = [m for m in candidates if m["id"] in session.candidate_ids]
        if len(narrowed) == 1:
            return _confirm_resolution(session, narrowed[0]["id"], user_response, db_path)
        if not narrowed:
            narrowed = candidates  # fallback: use new set

        summary = candidates_summary(narrowed)
        prompt = (
            "I'm still not sure which one. Could you check the bottle color?\n\n"
            f"{summary}\n\n"
            "Say the number or describe the bottle."
        )
        return ClarificationResult(
            outcome=ClarificationResult.NEEDS_CLARIFICATION,
            session_key=session.session_key,
            prompt=prompt,
        )

    # Cannot resolve — user said something unrecognizable
    if stripped.lower() in {"no", "i don't know", "not sure", "unsure", "skip", "cancel"}:
        _mark_session(session_key, "expired", db_path=db_path)
        log_id = log_uncertain(
            raw_transcript=user_response,
            notes="user could not clarify ambiguous med",
            db_path=db_path,
        )
        return ClarificationResult(
            outcome=ClarificationResult.UNRESOLVABLE,
            log_id=log_id,
            prompt="No problem. I've marked this as uncertain. Your caregiver will be notified.",
        )

    return _bad_clarification(session, user_response,
                              "I didn't understand that. Try saying the number (1 or 2) or the bottle color.", db_path)


def _confirm_resolution(
    session:        AmbiguitySession,
    med_id:         int,
    user_response:  str,
    db_path:        str,
) -> ClarificationResult:
    med = resolve_by_id(med_id, db_path=db_path)
    if not med:
        log_id = log_uncertain(raw_transcript=user_response,
                               notes="resolved med_id not found in DB", db_path=db_path)
        return ClarificationResult(outcome=ClarificationResult.UNRESOLVABLE, log_id=log_id)

    _mark_session(session.session_key, "resolved", resolved_med_id=med_id, db_path=db_path)
    return ClarificationResult(
        outcome=ClarificationResult.RESOLVED,
        session_key=session.session_key,
        medication=med,
    )


def _bad_clarification(
    session:       AmbiguitySession,
    user_response: str,
    reason:        str,
    db_path:       str,
) -> ClarificationResult:
    """Still ambiguous; return a corrective prompt without closing the session."""
    candidates = [resolve_by_id(cid, db_path=db_path) for cid in session.candidate_ids]
    candidates = [m for m in candidates if m]
    summary = candidates_summary(candidates)
    prompt = f"{reason}\n\n{summary}\n\nWhich one?"
    return ClarificationResult(
        outcome=ClarificationResult.NEEDS_CLARIFICATION,
        session_key=session.session_key,
        prompt=prompt,
    )
