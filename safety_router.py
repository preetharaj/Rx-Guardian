"""
safety_router.py — Deterministic safety pipeline.

Order of checks (LLM cannot override any of these):

  1. confidence_rules  → low confidence? ask repeat or log uncertain
  2. emergency check   → overdose/dangerous keywords? hard stop (Day 3)
  3. lookup            → 0 matches → unknown; 2+ → ambiguity_handler
  4. duplicate_guard   → already taken? block
  5. log MED_CONFIRMED

Returns a RouterResult with outcome + user-facing message.
"""

from __future__ import annotations

from typing import Optional

from lookup import lookup_medication, LookupStatus
from confidence_rules import check_confidence, ConfidenceResult
from duplicate_guard import check_duplicate, GuardResult
from ambiguity_handler import handle_ambiguous_input, ClarificationResult
from logging_events import (
    log_confirmed, log_uncertain, log_escalation, DB_PATH_DEFAULT
)
from emergency_escalation import check_escalation


class RouterOutcome:
    CONFIRMED         = "CONFIRMED"
    UNCERTAIN         = "UNCERTAIN"
    AMBIGUOUS         = "AMBIGUOUS"         # needs clarification turn
    DUPLICATE_BLOCKED = "DUPLICATE_BLOCKED"
    UNKNOWN_MED       = "UNKNOWN_MED"
    LOW_CONFIDENCE    = "LOW_CONFIDENCE"    # retry requested
    ESCALATION        = "ESCALATION"
    SUPPLEMENT        = "SUPPLEMENT"


class RouterResult:
    def __init__(
        self,
        outcome:         str,
        message:         str,
        medication:      Optional[dict] = None,
        session_key:     Optional[str]  = None,
        log_id:          Optional[int]  = None,
        block_log_id:    Optional[int]  = None,
        existing_log:    Optional[dict] = None,
    ):
        self.outcome      = outcome
        self.message      = message
        self.medication   = medication
        self.session_key  = session_key
        self.log_id       = log_id
        self.block_log_id = block_log_id
        self.existing_log = existing_log


def route(
    raw_transcript:   str,
    confidence_score: Optional[float] = None,
    retry_count:      int              = 0,
    input_mode:       str              = "text",
    db_path:          str              = DB_PATH_DEFAULT,
) -> RouterResult:
    """
    Run the full deterministic safety pipeline.
    Call this once per user turn; it handles all branching.
    """
    transcript_lower = raw_transcript.lower()

    # ── Step 1: Confidence check ──────────────────────────────────────────────
    conf = check_confidence(confidence_score, raw_transcript, retry_count, db_path)
    if conf.outcome == ConfidenceResult.RETRY:
        return RouterResult(
            outcome=RouterOutcome.LOW_CONFIDENCE,
            message=conf.prompt,
            log_id=conf.log_id,
        )
    if conf.outcome == ConfidenceResult.UNCERTAIN:
        return RouterResult(
            outcome=RouterOutcome.UNCERTAIN,
            message=conf.prompt,
            log_id=conf.log_id,
        )

    # ── Step 2: Emergency / unsafe-combo / wrong-route detection ────────────
    escalation = check_escalation(raw_transcript, db_path=db_path)
    if escalation is not None:
        return RouterResult(
            outcome=RouterOutcome.ESCALATION,
            message=escalation.message,
            log_id=escalation.log_id,
        )

    # ── Step 3: Uncertainty intent in transcript ──────────────────────────────
    _UNCERTAIN_PHRASES = {
        "not sure", "don't remember", "i think", "maybe",
        "forgot", "can't remember", "cannot remember", "unsure",
    }
    if any(ph in transcript_lower for ph in _UNCERTAIN_PHRASES):
        log_id = log_uncertain(
            raw_transcript=raw_transcript,
            confidence_score=confidence_score,
            notes="user expressed uncertainty in transcript",
            db_path=db_path,
        )
        return RouterResult(
            outcome=RouterOutcome.UNCERTAIN,
            message=(
                "I've noted that you're not sure if you took your medication.\n"
                "Please do not take another dose until your caregiver confirms.\n"
                "I've flagged this for them to check."
            ),
            log_id=log_id,
        )

    # ── Step 4: Medication lookup ─────────────────────────────────────────────
    # Strip common intent prefixes so "I took my heart pill" → "heart pill"
    lookup_query = _extract_med_query(raw_transcript)
    # Empty extract means the input was pronoun-only ("it", "that") — treat as unknown
    if not lookup_query:
        return RouterResult(
            outcome=RouterOutcome.UNKNOWN_MED,
            message=(
                "I didn't catch which medication you mean. "
                "Could you say the name or colour of the pill?"
            ),
        )
    result = lookup_medication(lookup_query, db_path=db_path)

    if result.status == LookupStatus.UNKNOWN:
        return RouterResult(
            outcome=RouterOutcome.UNKNOWN_MED,
            message=(
                f'I don\'t recognise "{raw_transcript}" in your medication list.\n'
                "Could you check the bottle name, or ask your caregiver to update the list?"
            ),
        )

    if result.status == LookupStatus.SUPPLEMENT:
        return RouterResult(
            outcome=RouterOutcome.SUPPLEMENT,
            message=(
                f'"{raw_transcript}" looks like a supplement, not a prescription.\n'
                "I won't record it as a dose. Ask your caregiver if you'd like to log supplements separately."
            ),
        )

    if result.status == LookupStatus.AMBIGUOUS:
        clarification = handle_ambiguous_input(
            raw_transcript=raw_transcript,
            candidates=result.candidates,
            confidence_score=confidence_score,
            db_path=db_path,
        )
        return RouterResult(
            outcome=RouterOutcome.AMBIGUOUS,
            message=clarification.prompt,
            session_key=clarification.session_key,
        )

    # ── Step 5: Duplicate guard ───────────────────────────────────────────────
    med = result.match
    guard = check_duplicate(med["id"], raw_transcript, db_path=db_path)

    if not guard.is_allowed:
        existing = guard.existing_log
        first_time = (existing or {}).get("logged_at", "earlier")[:16]
        return RouterResult(
            outcome=RouterOutcome.DUPLICATE_BLOCKED,
            message=(
                f"I already have a record that you took your {med['nickname'] or med['clinical_name']} "
                f"today at {first_time} UTC.\n"
                "You don't need to take it again.\n"
                "If you think there's a mistake, ask your caregiver to check."
            ),
            medication=med,
            existing_log=existing,
            block_log_id=guard.block_log_id,
        )

    # ── Step 6: Confirm ───────────────────────────────────────────────────────
    log_id = log_confirmed(
        medication_id=med["id"],
        raw_transcript=raw_transcript,
        confidence_score=confidence_score,
        input_mode=input_mode,
        db_path=db_path,
    )

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")

    return RouterResult(
        outcome=RouterOutcome.CONFIRMED,
        message=(
            f"Got it. I've recorded that you took your "
            f"{med['nickname'] or med['clinical_name']} "
            f"({med['clinical_name']}) at {now}.\n"
            "You don't need to log it again today."
        ),
        medication=med,
        log_id=log_id,
    )


# ─── Query extraction ─────────────────────────────────────────────────────────

import re as _re

_INTENT_PREFIXES = _re.compile(
    r"^(i\s+)?(just\s+|now\s+|already\s+)?(took|take|taking|had|have|eaten|swallowed|injected|done)\s+"
    r"(my\s+|the\s+|a\s+)?",
    _re.IGNORECASE,
)
_INTENT_PREFIXES_EXTENDED = _re.compile(
    r"^i\s+(want\s+to\s+take|will\s+take|need\s+to\s+take|am\s+going\s+to\s+take"
    r"|should\s+take|must\s+take|have\s+to\s+take|need\s+my|want\s+my)\s+"
    r"(my\s+|the\s+|a\s+)?",
    _re.IGNORECASE,
)
_TRAILING_NOISE = _re.compile(
    r"\s+(today|this morning|just now|already|again|earlier|a moment ago|too|as well)$",
    _re.IGNORECASE,
)

# Pronoun-only results after stripping — never a valid med query
_PRONOUN_ONLY = frozenset({"it", "that", "this", "them", "one", "same"})


def _extract_med_query(transcript: str) -> str:
    """
    Strip intent verbs, articles, possessives so lookup gets just the med reference.
    'I took my heart pill today' → 'heart pill'
    "I'm taking the white pill" → 'white pill'
    'the white pill' → 'white pill'
    'my white pill' → 'white pill'
    Falls back to original transcript if nothing matched.
    """
    q = transcript.strip()
    # "I'm taking / I am taking the ..."
    q = _re.sub(r"^i'?m\s+(taking|going to take|about to take)\s+(the\s+|my\s+|a\s+)?",
                "", q, flags=_re.IGNORECASE)
    q = _re.sub(r"^i\s+am\s+(taking|going to take|about to take)\s+(the\s+|my\s+|a\s+)?",
                "", q, flags=_re.IGNORECASE)
    # "I took / I had / I just took ..."
    q = _INTENT_PREFIXES_EXTENDED.sub("", q)
    q = _INTENT_PREFIXES.sub("", q)
    # Strip leading articles + possessives that survive the above
    # Strip leading articles/fillers (loop twice to catch "just the")
    for _ in range(2):
        q = _re.sub(r"^(the|my|a|an|just|only)\s+", "", q, flags=_re.IGNORECASE)
    # Strip trailing time references
    q = _TRAILING_NOISE.sub("", q)
    q = q.strip()
    # If stripping left only a pronoun, the query is ambiguous — return empty to force UNKNOWN
    if not q or q.lower() in _PRONOUN_ONLY:
        return ""
    return q
