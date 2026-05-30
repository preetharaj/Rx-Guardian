"""
confidence_rules.py — Deterministic low-confidence handling.

Rules:
  - confidence_score < threshold → do NOT confirm; ask repeat.
  - confidence_score is None     → treat as full confidence (typed input).
  - After MAX_RETRIES failed attempts → log MED_UNCERTAIN, notify caregiver.
  - Threshold read from patient_profile; falls back to DEFAULT_THRESHOLD.
  - LLM score is advisory only; safety logic here is authoritative.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from logging_events import log_low_confidence, log_uncertain, DB_PATH_DEFAULT

DEFAULT_THRESHOLD = 0.75
MAX_RETRIES       = 2   # attempts before giving up and logging MED_UNCERTAIN


# ─── ConfidenceResult ────────────────────────────────────────────────────────

class ConfidenceResult:
    PASS      = "PASS"      # score acceptable; proceed
    RETRY     = "RETRY"     # too low; ask user to repeat
    UNCERTAIN = "UNCERTAIN" # retries exhausted; log uncertain

    def __init__(
        self,
        outcome:    str,
        prompt:     Optional[str] = None,
        log_id:     Optional[int] = None,
        score:      Optional[float] = None,
        threshold:  Optional[float] = None,
    ):
        self.outcome   = outcome
        self.prompt    = prompt
        self.log_id    = log_id
        self.score     = score
        self.threshold = threshold

    @property
    def is_pass(self) -> bool:
        return self.outcome == self.PASS


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_threshold(db_path: str) -> float:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT value FROM patient_profile WHERE key = 'confidence_threshold'")
    row = cur.fetchone()
    con.close()
    if row:
        try:
            return float(row[0])
        except (ValueError, TypeError):
            pass
    return DEFAULT_THRESHOLD


# ─── Core check ──────────────────────────────────────────────────────────────

def check_confidence(
    confidence_score: Optional[float],
    raw_transcript:   str,
    retry_count:      int = 0,
    db_path:          str = DB_PATH_DEFAULT,
) -> ConfidenceResult:
    """
    Evaluate transcript confidence before proceeding.

    Args:
        confidence_score: Float 0.0–1.0 from STT/NLU, or None for typed input.
        raw_transcript:   Verbatim user input (always logged).
        retry_count:      How many low-confidence retries already attempted.
        db_path:          Database path.

    Returns ConfidenceResult with outcome PASS / RETRY / UNCERTAIN.
    """
    # None = typed input, assume full confidence
    if confidence_score is None:
        return ConfidenceResult(
            outcome=ConfidenceResult.PASS,
            score=None,
            threshold=None,
        )

    # Score must be numeric 0–1
    if not isinstance(confidence_score, (int, float)):
        return ConfidenceResult(
            outcome=ConfidenceResult.PASS,  # fail-open for non-numeric: let lookup decide
            score=confidence_score,
        )

    threshold = _get_threshold(db_path)

    if confidence_score >= threshold:
        return ConfidenceResult(
            outcome=ConfidenceResult.PASS,
            score=confidence_score,
            threshold=threshold,
        )

    # Below threshold ─────────────────────────────────────────────────────────
    log_id = log_low_confidence(raw_transcript, confidence_score, db_path=db_path)

    if retry_count < MAX_RETRIES:
        attempt_word = {0: "once more", 1: "one more time"}.get(retry_count, "again")
        prompt = (
            "Sorry, I didn't catch that clearly. "
            f"Could you say which medication you took {attempt_word}?\n"
            "Speak slowly and include the pill colour or name if you can."
        )
        return ConfidenceResult(
            outcome=ConfidenceResult.RETRY,
            prompt=prompt,
            log_id=log_id,
            score=confidence_score,
            threshold=threshold,
        )

    # Retries exhausted → MED_UNCERTAIN
    uncertain_id = log_uncertain(
        raw_transcript=raw_transcript,
        notes=f"low confidence after {MAX_RETRIES} retries; score={confidence_score:.2f}",
        db_path=db_path,
    )
    return ConfidenceResult(
        outcome=ConfidenceResult.UNCERTAIN,
        prompt=(
            "I wasn't able to understand clearly after a few tries. "
            "I've marked this dose as uncertain. "
            "Please ask your caregiver to check."
        ),
        log_id=uncertain_id,
        score=confidence_score,
        threshold=threshold,
    )


# ─── Convenience: assess without logging ─────────────────────────────────────

def score_is_acceptable(
    confidence_score: Optional[float],
    db_path:          str = DB_PATH_DEFAULT,
) -> bool:
    """
    Quick boolean check — does NOT write any log rows.
    Use for pre-flight checks only; use check_confidence() for real flow.
    """
    if confidence_score is None:
        return True
    threshold = _get_threshold(db_path)
    return float(confidence_score) >= threshold
